#!/usr/bin/env python3
"""Сервис VPN. Единственная часть панели, работающая от root.

Панель и загруженные сайты работают без прав и могут обращаться к этому сокету,
поэтому всё, что сюда приходит, считается недоверенным:

- awg-quick выполняет через bash значения PreUp, PostUp, PreDown и PostDown. Без их удаления
  ключ стал бы способом выполнить любую команду от root. Остальные параметры он передаёт
  в `awg setconf`, который команд не выполняет.
- значения проверяются по списку разрешённых символов;
- приватный ключ хранится только здесь и в ответах не возвращается.

Запускается как `python3 -I`, поэтому использует только стандартную библиотеку и один файл.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import zlib

INTERFACE = "malinnik0"
CONF_DIR = "/etc/amnezia/amneziawg"
CONF_PATH = f"{CONF_DIR}/{INTERFACE}.conf"
STATE_PATH = "/var/lib/malinnik-vpn/state.json"
SOCKET_PATH = "/run/malinnik-vpn/vpn.sock"
SOCKET_GROUP = "malinnik"

MAX_KEY = 64 * 1024
MAX_DECOMPRESSED = 1024 * 1024
RETRY_SECONDS = 30
EXEC_KEYS = {"preup", "predown", "postup", "postdown", "saveconfig", "table"}
SAFE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*$", re.A)
SAFE_VALUE = re.compile(r"^[\w .:,/+=\-\[\]<>]*$", re.A)
WG_KEY = re.compile(r"^[A-Za-z0-9+/]{43}=$")
ENDPOINT = re.compile(r"^(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+):(\d{1,5})$")


class KeyProblem(Exception):
    """Сообщение показывается пользователю."""


# --- разбор ключа ---


def _decode_vpn_uri(text: str) -> dict:
    payload = text[len("vpn://"):].strip()
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except ValueError:
        raise KeyProblem("Ключ vpn:// повреждён: скопируй его целиком.") from None
    # Обычно это Qt qCompress: 4 байта длины и поток zlib. Встречается и JSON без сжатия.
    for candidate in (raw[4:], raw):
        try:
            inflater = zlib.decompressobj()
            data = inflater.decompress(candidate, MAX_DECOMPRESSED)
            if inflater.unconsumed_tail:
                raise KeyProblem("Ключ vpn:// слишком большой.")
            return json.loads(data)
        except (zlib.error, ValueError):
            continue
    try:
        return json.loads(raw)
    except ValueError:
        raise KeyProblem("Ключ vpn:// повреждён: скопируй его целиком.") from None


def key_to_conf(text: str) -> str:
    """Превращает ключ vpn:// в текст конфига. Обычный конфиг возвращает как есть."""
    text = text.strip().lstrip("﻿")
    if not text.lower().startswith("vpn://"):
        return text
    data = _decode_vpn_uri(text)
    if not isinstance(data, dict):
        raise KeyProblem("Ключ vpn:// повреждён: скопируй его целиком.")
    containers = data.get("containers") or []
    awg = next((c["awg"] for c in containers if isinstance(c, dict) and isinstance(c.get("awg"), dict)), None)
    if awg is None:
        raise KeyProblem(
            "В этом ключе нет настроек AmneziaWG. Ключи платных серверов Amnezia настройки не содержат: "
            "нужен файл .conf или ключ от своего сервера."
        )
    try:
        last = awg["last_config"]
        last = json.loads(last) if isinstance(last, str) else last
        conf = last["config"]
    except (KeyError, TypeError, ValueError):
        raise KeyProblem("В ключе vpn:// нет готового конфига AmneziaWG.") from None
    conf = conf.replace("$PRIMARY_DNS", str(data.get("dns1") or "1.1.1.1"))
    conf = conf.replace("$SECONDARY_DNS", str(data.get("dns2") or "1.0.0.1"))
    mtu = last.get("mtu")
    if mtu and not re.search(r"^\s*MTU\s*=", conf, re.M | re.I):
        conf = re.sub(r"^\s*\[Interface\]\s*$", f"[Interface]\nMTU = {mtu}", conf, count=1, flags=re.M | re.I)
    return conf


def _items(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def sanitize(conf: str) -> dict:
    """Проверяет конфиг и собирает его заново только из разрешённого."""
    sections: list[tuple[str, list[tuple[str, str]]]] = []
    removed: list[str] = []
    for raw in conf.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            name = line.strip("[] ").lower()
            if name not in ("interface", "peer"):
                raise KeyProblem(f"Неизвестный раздел конфига: {line[:40]}")
            sections.append((name, []))
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not sections or not SAFE_KEY.match(key):
            raise KeyProblem(f"Не понял строку конфига: {key[:40]}")
        if key.lower() in EXEC_KEYS:
            removed.append(key)
            continue
        if not SAFE_VALUE.match(value):
            raise KeyProblem(f"Недопустимые символы в параметре {key}.")
        sections[-1][1].append((key, value))

    interfaces = [items for name, items in sections if name == "interface"]
    peers = [items for name, items in sections if name == "peer"]
    if len(interfaces) != 1 or not peers:
        raise KeyProblem("Это не конфиг AmneziaWG: нужны разделы [Interface] и [Peer].")

    def values(items, wanted):
        return [v for k, v in items if k.lower() == wanted]

    interface = interfaces[0]
    private = values(interface, "privatekey")
    if len(private) != 1 or not WG_KEY.match(private[0]):
        raise KeyProblem("В конфиге нет корректного приватного ключа (PrivateKey).")
    addresses = [a for v in values(interface, "address") for a in _items(v)]
    if not addresses:
        raise KeyProblem("В конфиге нет адреса (Address).")
    for address in addresses:
        try:
            ipaddress.ip_interface(address)
        except ValueError:
            raise KeyProblem(f"Неверный адрес: {address}") from None
    for mtu in values(interface, "mtu"):
        if not mtu.isdigit():
            raise KeyProblem("Неверное значение MTU.")

    endpoint = None
    allowed: list[str] = []
    for peer in peers:
        public = values(peer, "publickey")
        if len(public) != 1 or not WG_KEY.match(public[0]):
            raise KeyProblem("В конфиге нет корректного ключа сервера (PublicKey).")
        for shared in values(peer, "presharedkey"):
            if not WG_KEY.match(shared):
                raise KeyProblem("Неверный PresharedKey.")
        for candidate in values(peer, "endpoint"):
            match = ENDPOINT.match(candidate)
            if not match or not 0 < int(match.group(2)) < 65536:
                raise KeyProblem(f"Неверный адрес сервера: {candidate}")
            endpoint = endpoint or candidate
        for network in (n for v in values(peer, "allowedips") for n in _items(v)):
            try:
                ipaddress.ip_network(network, strict=False)
            except ValueError:
                raise KeyProblem(f"Неверная сеть в AllowedIPs: {network}") from None
            allowed.append(network)
    if endpoint is None:
        raise KeyProblem("В конфиге нет адреса сервера (Endpoint).")
    if not allowed:
        raise KeyProblem("В конфиге не указано, что пускать через VPN (AllowedIPs).")

    warnings = []
    if removed:
        warnings.append("Из ключа убраны команды: " + ", ".join(sorted(set(removed))) + ". Они небезопасны.")

    lines = []
    for name, items in sections:
        lines.append("[Interface]" if name == "interface" else "[Peer]")
        lines.extend(f"{key} = {value}" for key, value in items)
        lines.append("")
    return {
        "conf": "\n".join(lines),
        "warnings": warnings,
        "endpoint": endpoint,
        "addresses": addresses,
        "allowed": allowed,
        "has_dns": bool(values(interface, "dns")),
        "full_tunnel": any(ipaddress.ip_network(n, strict=False).prefixlen == 0 for n in allowed),
    }


def drop_dns(conf: str) -> str:
    return "\n".join(line for line in conf.splitlines() if not re.match(r"^DNS\s*=", line, re.I)) + "\n"


def lan_conflict(allowed: list[str], local: list[str]) -> str | None:
    """Находит сеть VPN, которая отрежет малину от домашней сети, а с ней и панель.

    0.0.0.0/0 безопасен: awg-quick оставляет локальные маршруты. Опасна только сеть
    такой же или большей точности, как домашняя: она выиграет выбор маршрута.
    """
    for item in allowed:
        network = ipaddress.ip_network(item, strict=False)
        for lan in local:
            home = ipaddress.ip_network(lan, strict=False)
            if network.version == home.version and network.overlaps(home) and network.prefixlen >= home.prefixlen:
                return f"AllowedIPs содержит {item} и пересекается с домашней сетью {lan}: малина пропала бы из сети."
    return None


# --- работа с системой ---


def run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


def local_networks() -> list[str]:
    try:
        result = run(["ip", "-j", "route", "show", "table", "main", "scope", "link"], timeout=5)
        routes = json.loads(result.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [r["dst"] for r in routes if r.get("dst", "default") != "default" and r.get("dev") != INTERFACE]


def interface_up() -> bool:
    return os.path.exists(f"/sys/class/net/{INTERFACE}")


def installed() -> bool:
    return bool(shutil.which("awg-quick") and shutil.which("awg"))


def _short_error(result: subprocess.CompletedProcess) -> str:
    lines = [line for line in (result.stderr or "").splitlines() if not line.startswith("[#]")]
    return " ".join(lines)[-300:] or f"код {result.returncode}"


class Service:
    def __init__(self) -> None:
        self.desired = self._load_desired()
        self.last_error: str | None = None
        self.warnings: list[str] = []
        self.next_retry = 0.0

    @staticmethod
    def _load_desired() -> bool:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return bool(json.load(f).get("up"))
        except (OSError, ValueError):
            return False

    def _save_desired(self, up: bool) -> None:
        self.desired = up
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"up": up}, f)

    @staticmethod
    def _summary() -> dict | None:
        try:
            with open(CONF_PATH, encoding="utf-8") as f:
                return sanitize(f.read())
        except (OSError, KeyProblem):
            return None

    def _bring_up(self) -> None:
        if interface_up():
            return
        result = run(["awg-quick", "up", CONF_PATH])
        if result.returncode != 0:
            raise KeyProblem(f"VPN не поднялся: {_short_error(result)}")

    def _bring_down(self) -> None:
        if interface_up():
            result = run(["awg-quick", "down", CONF_PATH])
            if result.returncode != 0:
                raise KeyProblem(f"VPN не выключился: {_short_error(result)}")

    def up(self) -> dict:
        if not installed():
            raise KeyProblem("AmneziaWG не установлен. Запусти install.sh на малине ещё раз.")
        if not os.path.exists(CONF_PATH):
            raise KeyProblem("Сначала добавь ключ.")
        self.last_error = None
        self._bring_up()
        self._save_desired(True)
        return self.status()

    def down(self) -> dict:
        self._save_desired(False)
        self.last_error = None
        self._bring_down()
        return self.status()

    def import_key(self, text: str) -> dict:
        if not isinstance(text, str) or not text.strip():
            raise KeyProblem("Вставь ключ или содержимое файла .conf.")
        if len(text) > MAX_KEY:
            raise KeyProblem("Ключ слишком большой.")
        clean = sanitize(key_to_conf(text))
        conflict = lan_conflict(clean["allowed"], local_networks())
        if conflict:
            raise KeyProblem(conflict)

        conf = clean["conf"]
        warnings = clean["warnings"]
        if clean["has_dns"] and not shutil.which("resolvconf"):
            # Без resolvconf awg-quick падает на настройке DNS и VPN не поднимается вовсе.
            conf = drop_dns(conf)
            warnings = warnings + ["DNS из ключа не применён: на малине нет resolvconf. Запросы идут через обычный DNS."]

        was_up = interface_up()
        self._bring_down()
        os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
        fd = os.open(CONF_PATH + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(conf)
        os.replace(CONF_PATH + ".tmp", CONF_PATH)
        self.warnings = warnings
        self.last_error = None
        if was_up:
            self._bring_up()
        return self.status()

    def delete(self) -> dict:
        self._save_desired(False)
        self._bring_down()
        if os.path.exists(CONF_PATH):
            os.unlink(CONF_PATH)
        self.warnings = []
        self.last_error = None
        return self.status()

    @staticmethod
    def _peer_numbers() -> tuple[int | None, int, int]:
        """Возраст последнего обмена ключами и трафик. Приватный ключ сюда не попадает."""
        age = None
        rx = tx = 0
        try:
            stamps = run(["awg", "show", INTERFACE, "latest-handshakes"], timeout=5).stdout
            for line in stamps.splitlines():
                stamp = int(line.split("\t")[1])
                if stamp:
                    current = max(0, int(time.time()) - stamp)
                    age = current if age is None else min(age, current)
            transfer = run(["awg", "show", INTERFACE, "transfer"], timeout=5).stdout
            for line in transfer.splitlines():
                parts = line.split("\t")
                rx += int(parts[1])
                tx += int(parts[2])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            pass
        return age, rx, tx

    def status(self) -> dict:
        summary = self._summary()
        up = interface_up()
        result = {
            "installed": installed(),
            "configured": summary is not None,
            "desired": self.desired,
            "up": up,
            "endpoint": summary["endpoint"] if summary else None,
            "addresses": summary["addresses"] if summary else [],
            "full_tunnel": summary["full_tunnel"] if summary else False,
            "warnings": self.warnings,
            "problem": self.last_error,
            "handshake_age": None,
            "rx": 0,
            "tx": 0,
        }
        if up:
            result["handshake_age"], result["rx"], result["tx"] = self._peer_numbers()
        return result

    def handle(self, request: dict) -> dict:
        command = request.get("cmd")
        try:
            if command == "status":
                return self.status()
            if command == "up":
                return self.up()
            if command == "down":
                return self.down()
            if command == "import":
                return self.import_key(request.get("text"))
            if command == "delete":
                return self.delete()
            return {"error": "Неизвестная команда."}
        except KeyProblem as exc:
            return {"error": str(exc)}
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": f"Ошибка системы: {exc}"}

    def keep_alive(self) -> None:
        """Поднимает VPN после перезагрузки малины и если он пропал сам."""
        if not self.desired or interface_up() or time.time() < self.next_retry:
            return
        self.next_retry = time.time() + RETRY_SECONDS
        try:
            self._bring_up()
            self.last_error = None
        except (KeyProblem, OSError, subprocess.SubprocessError) as exc:
            self.last_error = str(exc)


def _serve_client(service: Service, conn: socket.socket) -> None:
    conn.settimeout(5)
    data = b""
    while b"\n" not in data and len(data) < MAX_KEY * 2:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    try:
        request = json.loads(data)
        if not isinstance(request, dict):
            raise ValueError
    except ValueError:
        response = {"error": "Неверный запрос."}
    else:
        response = service.handle(request)
    conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8"))


def main() -> None:
    import grp

    if os.geteuid() != 0:
        sys.exit("Сервис VPN должен работать от root.")
    os.umask(0o077)
    service = Service()
    # В Docker папку не создаёт systemd. Панель должна видеть её при umask 077.
    socket_dir = os.path.dirname(SOCKET_PATH)
    os.makedirs(socket_dir, exist_ok=True)
    os.chmod(socket_dir, 0o755)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chown(SOCKET_PATH, 0, grp.getgrnam(SOCKET_GROUP).gr_gid)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(8)
    server.settimeout(5)
    while True:
        service.keep_alive()
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        with conn:
            try:
                _serve_client(service, conn)
            except OSError:
                pass


if __name__ == "__main__":
    main()
