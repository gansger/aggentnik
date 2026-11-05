"""Пароль панели, сессии и защита от перебора."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

SESSION_TTL = 30 * 24 * 3600
MAX_FAILURES = 5
LOCK_SECONDS = 60
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024}


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)


def _config_path(data_dir: str) -> str:
    return os.path.join(data_dir, "config.json")


def has_password(data_dir: str) -> bool:
    return os.path.exists(_config_path(data_dir))


def set_password(data_dir: str, password: str) -> None:
    os.makedirs(data_dir, exist_ok=True)
    salt = secrets.token_bytes(16)
    payload = {
        "salt": base64.b64encode(salt).decode(),
        "hash": base64.b64encode(_hash(password, salt)).decode(),
    }
    path = _config_path(data_dir)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
    os.replace(path + ".tmp", path)


def generate_password() -> str:
    return secrets.token_urlsafe(12)


class Auth:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, tuple[int, float]] = {}
        self.lock = threading.Lock()

    def locked_for(self, ip: str) -> int:
        with self.lock:
            count, until = self.failures.get(ip, (0, 0.0))
        return max(0, int(until - time.time())) if count >= MAX_FAILURES else 0

    def login(self, ip: str, password: str) -> str | None:
        """Возвращает токен сессии или None, если пароль неверный."""
        with open(_config_path(self.data_dir)) as f:
            config = json.load(f)
        salt = base64.b64decode(config["salt"])
        expected = base64.b64decode(config["hash"])
        ok = hmac.compare_digest(_hash(password, salt), expected)
        with self.lock:
            if not ok:
                count = self.failures.get(ip, (0, 0.0))[0] + 1
                self.failures[ip] = (count, time.time() + LOCK_SECONDS)
                return None
            self.failures.pop(ip, None)
            token = secrets.token_urlsafe(32)
            self.sessions[self._key(token)] = time.time() + SESSION_TTL
            return token

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        key = self._key(token)
        with self.lock:
            expires = self.sessions.get(key)
            if expires is None:
                return False
            if expires < time.time():
                del self.sessions[key]
                return False
            return True

    def logout(self, token: str | None) -> None:
        if token:
            with self.lock:
                self.sessions.pop(self._key(token), None)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
