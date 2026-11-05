"""Уведомления в Telegram. Сообщения уходят из очереди и переживают обрывы связи."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Callable

log = logging.getLogger("malinnik")

API = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")
CHAT_RE = re.compile(r"^-?\d+$")
MAX_AGE = 3600
RETRY_SECONDS = 30
POLL_SECONDS = 25
POLL_PAUSE = 5
POLL_PAUSE_LONG = 60
FIND_CHAT_WAIT = 3
CHAT_FRESH = 600
UPDATE_TYPES = ["message", "callback_query", "channel_post", "my_chat_member"]


class TelegramError(Exception):
    """Сообщение показывается пользователю. permanent — повторять отправку бессмысленно."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def _chat_of(update: dict) -> dict | None:
    query = update.get("callback_query") or {}
    source = (update.get("message") or update.get("channel_post")
              or update.get("my_chat_member") or query.get("message") or {})
    return source.get("chat")


class Telegram:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "telegram.json")
        self.settings = {"token": "", "chat_id": "", "enabled": False}
        self.chat_name = ""
        self.last_error: str | None = None
        self.poll_error: str | None = None
        self.last_chat: tuple[float, dict] | None = None
        self.handler: Callable[[dict], None] | None = None
        self.commands: list[dict] = []
        self.queue: deque = deque(maxlen=50)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.closing = threading.Event()
        try:
            with open(self.path, encoding="utf-8") as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass

    def run(self) -> None:
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._poller, daemon=True).start()

    def shutdown(self) -> None:
        self.closing.set()
        self.wake.set()

    def _save(self) -> None:
        fd = os.open(self.path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.settings, f)
        os.replace(self.path + ".tmp", self.path)

    def to_json(self) -> dict:
        """Токен в браузер не отдаём: достаточно знать, что он сохранён."""
        return {
            "token_set": bool(self.settings["token"]),
            "chat_id": self.settings["chat_id"],
            "chat_name": self.chat_name,
            "enabled": self.settings["enabled"],
            "error": self.last_error or self.poll_error,
        }

    def update(self, changes: dict) -> None:
        settings = dict(self.settings)
        token = str(changes.get("token", "")).strip()
        if token:
            if not TOKEN_RE.match(token):
                raise TelegramError("Токен выглядит неверно. Его выдаёт @BotFather, вид: 123456:ABC-DEF…", True)
            if token != settings["token"]:
                settings["chat_id"] = ""  # чат привязан к конкретному боту
                self.chat_name = ""
                self.last_chat = None
            settings["token"] = token
        if "chat_id" in changes:
            chat_id = str(changes["chat_id"]).strip()
            if chat_id and not CHAT_RE.match(chat_id):
                raise TelegramError("Номер чата должен состоять из цифр.", True)
            settings["chat_id"] = chat_id
        if "enabled" in changes:
            settings["enabled"] = bool(changes["enabled"])
        if settings["enabled"] and not (settings["token"] and settings["chat_id"]):
            raise TelegramError("Сначала укажи токен бота и найди чат.", True)
        self.settings = settings
        self.last_error = None
        self._save()

    def _call(self, method: str, payload: dict, timeout: float = 15) -> object:
        token = self.settings["token"]
        if not token:
            raise TelegramError("Сначала укажи токен бота.", True)
        request = urllib.request.Request(
            f"{API}/bot{token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)["result"]
        except urllib.error.HTTPError as exc:
            with exc:
                if exc.code == 401:
                    raise TelegramError("Telegram не принял токен. Проверь его у @BotFather.", True) from None
                try:
                    description = json.load(exc).get("description", "")
                except (ValueError, OSError):
                    description = ""
            if "chat not found" in description:
                raise TelegramError("Чат не найден. Напиши боту и нажми «Найти мой чат».", True) from None
            if exc.code == 409:
                raise TelegramError(
                    "Этого бота уже опрашивает другая программа. У одного бота может быть только одна панель.", True,
                ) from None
            permanent = 400 <= exc.code < 500 and exc.code != 429
            raise TelegramError(f"Telegram ответил ошибкой {exc.code}: {description}", permanent) from None
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            # В исключении может оказаться адрес с токеном, поэтому его не показываем.
            raise TelegramError("Нет связи с Telegram.") from None

    def find_chat(self) -> str:
        """Привязывает бота к чату, который только что ему написал.

        Сообщения читает только _poller: Telegram не даёт двум запросам getUpdates идти
        одновременно. Старые чаты не берём, чтобы не привязать того, кто писал боту раньше.
        """
        if not self.settings["token"]:
            raise TelegramError("Сначала укажи токен бота.", True)
        deadline = time.time() + FIND_CHAT_WAIT
        while time.time() < deadline:
            seen = self.last_chat
            if seen and time.time() - seen[0] < CHAT_FRESH:
                chat = seen[1]
                self.settings["chat_id"] = str(chat["id"])
                self.chat_name = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
                self.last_error = None
                self._save()
                return self.chat_name or self.settings["chat_id"]
            time.sleep(0.2)
        raise TelegramError("Бот пока не получал сообщений. Напиши ему что угодно и нажми ещё раз.", True)

    def _register_commands(self) -> None:
        if self.commands:
            try:
                self._call("setMyCommands", {"commands": self.commands})
            except TelegramError as exc:
                log.warning("Telegram: не удалось задать меню команд: %s", exc)

    def _poll_once(self, offset: int | None) -> list:
        payload = {"timeout": POLL_SECONDS, "allowed_updates": UPDATE_TYPES}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, POLL_SECONDS + 10)

    def _poller(self) -> None:
        """Постоянно забирает сообщения боту. Работает, как только задан токен."""
        token_seen = None
        offset = None
        while not self.closing.is_set():
            token = self.settings["token"]
            if not token:
                self.closing.wait(2)
                continue
            if token != token_seen:
                token_seen, offset = token, None
                self.poll_error = None
                self._register_commands()
            try:
                updates = self._poll_once(offset)
            except TelegramError as exc:
                if exc.permanent:
                    self.poll_error = str(exc)
                    log.warning("Telegram: %s", exc)
                    self.closing.wait(POLL_PAUSE_LONG)
                else:
                    self.closing.wait(POLL_PAUSE)
                continue
            self.poll_error = None
            if self.settings["token"] != token:
                continue  # пока шёл запрос, токен сменили: это сообщения прежнего бота
            for update in updates:
                offset = update["update_id"] + 1
                self._receive(update)

    def _receive(self, update: dict) -> None:
        chat = _chat_of(update)
        if chat:
            self.last_chat = (time.time(), chat)
        if self.handler:
            self.handler(update)

    def send_test(self) -> None:
        if not self.settings["chat_id"]:
            raise TelegramError("Сначала найди чат.", True)
        self._send("Аггентник: уведомления работают.")
        self.last_error = None

    def _send(self, text: str) -> None:
        self._call("sendMessage", {"chat_id": self.settings["chat_id"], "text": text})

    def notify(self, text: str) -> None:
        if not (self.settings["enabled"] and self.settings["token"] and self.settings["chat_id"]):
            return
        with self.lock:
            self.queue.append((time.time(), text))
        self.wake.set()

    def _worker(self) -> None:
        while not self.closing.is_set():
            self.wake.wait(RETRY_SECONDS)
            self.wake.clear()
            self._drain()

    def _drain(self) -> None:
        while not self.closing.is_set():
            with self.lock:
                if not self.queue:
                    return
                created, text = self.queue[0]
            if time.time() - created < MAX_AGE and self.settings["enabled"]:
                try:
                    self._send(text)
                    self.last_error = None
                except TelegramError as exc:
                    self.last_error = str(exc)
                    if not exc.permanent:
                        return  # связи нет: оставляем в очереди до следующей попытки
                    log.warning("Telegram: %s", exc)
            with self.lock:
                if self.queue and self.queue[0][1] == text:
                    self.queue.popleft()
