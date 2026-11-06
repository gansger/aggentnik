"""Публикация и Telegram. Внешние сервисы подменяются."""
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from malinnik import alerts, bot, sites, telegram, tunnel  # noqa: E402


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.alerts = alerts.Alerts(self.sent.append)
        self.now = 1000.0
        patch = mock.patch.object(alerts.time, "time", lambda: self.now)
        patch.start()
        self.addCleanup(patch.stop)

    def check(self, state="running", desired="running", system=None, sites=None):
        site = {"id": "a", "name": "Сайт", "state": state, "desired": desired, "exit_code": 1}
        self.alerts.check([site] if sites is None else sites, system or {})

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
            "sites": self.sites,
        }
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
        for part in ("48 °C", "Не хватает питания", "Магазин: работает", "Блог: упал", "порт 8100"):
            self.assertIn(part, text)

    def test_keyboard_words_and_bot_suffix(self):
        self.bot.handle(self.message("Статус"))
        self.bot.handle(self.message("/status@my_bot"))
        self.assertEqual(len(self.texts()), 2)
        self.assertIn("Малина:", self.texts()[1])

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
        for data in ("do:stop:shop", "do:publish:shop", "do:unpublish:shop", "yes:выдуманный"):
            self.bot.handle(self.press(data))
        self.manager.action.assert_not_called()

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
