"""Обращение панели к сервису VPN (vpn_helper.py), который работает от root."""
from __future__ import annotations

import json
import socket

SOCKET_PATH = "/run/malinnik-vpn/vpn.sock"
UNAVAILABLE = {"available": False}
MAX_RESPONSE = 256 * 1024


class VpnError(Exception):
    """Сообщение показывается пользователю."""


def call(command: str, **args) -> dict:
    if not hasattr(socket, "AF_UNIX"):
        raise VpnError("VPN работает только на малине.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(90)  # поднятие VPN занимает до минуты
            sock.connect(SOCKET_PATH)
            sock.sendall(json.dumps({"cmd": command, **args}).encode("utf-8") + b"\n")
            data = b""
            while len(data) < MAX_RESPONSE:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError:
        raise VpnError("Сервис VPN не запущен. Запусти install.sh на малине ещё раз.") from None
    try:
        response = json.loads(data)
    except ValueError:
        raise VpnError("Сервис VPN ответил непонятно.") from None
    if response.get("error"):
        raise VpnError(response["error"])
    return response


def status() -> dict:
    try:
        return {"available": True, **call("status")}
    except VpnError:
        return dict(UNAVAILABLE)
