"""Публикация сайта в интернет через быстрый туннель Cloudflare.

Туннель ведёт только на порт сайта, поэтому сама панель из интернета недоступна.
Аккаунт и домен не нужны, но адрес случайный и меняется при перезапуске туннеля.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable

from .procs import LogFile, kill, spawn

log = logging.getLogger("malinnik")

# api.trycloudflare.com встречается в сообщениях об ошибках, это не адрес сайта.
URL_RE = re.compile(rb"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
INSTALL_HINT = "cloudflared не установлен. Запусти install.sh на малине ещё раз."
STABLE_AFTER = 60
MAX_DELAY = 60


def available() -> bool:
    return shutil.which("cloudflared") is not None


def command(port: int) -> str:
    return f"cloudflared tunnel --no-autoupdate --url http://localhost:{port}"


class Tunnel:
    def __init__(self, log_path: str, on_url: Callable[[str], None] | None = None):
        self.log = LogFile(log_path)
        self.on_url = on_url
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.url: str | None = None
        self.wanted = False
        self.started_at = 0.0
        self.restart_at: float | None = None
        self.failures = 0

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.log.write(line)
            match = URL_RE.search(line)
            if match and self.proc is proc:
                url = match.group().decode()
                if url != self.url:
                    self.url = url
                    if self.on_url:
                        try:
                            self.on_url(url)
                        except Exception:  # noqa: BLE001 — уведомление не должно ломать туннель
                            log.exception("Ошибка уведомления об адресе туннеля")
        proc.stdout.close()

    def _launch(self) -> None:
        self.url = None
        self.restart_at = None
        self.started_at = time.time()
        env = {k: v for k, v in os.environ.items() if not k.startswith("MALINNIK_")}
        self.log.line(f"\n[аггентник] публикация порта {self.port}")
        self.proc = spawn(command(self.port), os.path.dirname(self.log.path), env)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def start(self, port: int) -> None:
        with self.lock:
            self._stop_proc()
            self.wanted = True
            self.port = port
            self.failures = 0
            self._launch()

    def _stop_proc(self) -> None:
        proc, self.proc = self.proc, None
        self.url = None
        self.restart_at = None
        if proc:
            kill(proc)

    def stop(self) -> None:
        with self.lock:
            self.wanted = False
            self._stop_proc()

    def retarget(self, port: int) -> None:
        """Сайт стал слушать другой порт: туннель надо направить туда."""
        with self.lock:
            if self.wanted and port != self.port:
                self.start(port)

    def watch(self) -> None:
        """Вызывается раз в секунду: поднимает упавший туннель с нарастающей паузой."""
        with self.lock:
            if not self.wanted:
                return
            now = time.time()
            if self.proc is None:
                if self.restart_at and now >= self.restart_at:
                    self._launch()
                return
            code = self.proc.poll()
            if code is None:
                return
            self.proc = None
            self.url = None
            self.failures = 1 if now - self.started_at > STABLE_AFTER else self.failures + 1
            self.restart_at = now + min(MAX_DELAY, 2 ** self.failures)
            self.log.line(f"[аггентник] туннель завершился с кодом {code}, повторю позже")

    def to_json(self) -> dict:
        if not self.wanted:
            state = "off"
        elif self.url:
            state = "online"
        elif self.proc:
            state = "starting"
        else:
            state = "retrying"
        return {"state": state, "url": self.url}
