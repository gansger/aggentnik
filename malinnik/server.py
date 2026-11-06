"""HTTP-сервер панели: страница и JSON API."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .auth import SESSION_TTL, Auth
from .bot import Bot
from .sites import DISK_RESERVE, Manager, SiteError
from .telegram import TelegramError

log = logging.getLogger("malinnik")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
COOKIE = "malinnik_session"
MAX_JSON = 1024 * 1024
CHUNK = 1024 * 1024
MAX_DRAIN = 64 * 1024 * 1024
MAX_COVER = 5 * 1024 * 1024
SITE_ROUTE = re.compile(r"^/api/sites/([a-z0-9-]+)(?:/([a-z]+))?$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "Cache-Control": "no-cache",
}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = f"Malinnik/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 300

    manager: Manager
    auth: Auth

    def log_message(self, format, *args):
        log.debug("%s %s", self.client_address[0], format % args)

    # --- ответы ---

    def _respond(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8", extra)

    # --- запрос ---

    def _token(self) -> str | None:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ApiError(400, "Неверная длина запроса.") from None
        if length > MAX_JSON:
            raise ApiError(413, "Слишком большой запрос.")
        self.body_read = True
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Неверный JSON.") from None
        if not isinstance(data, dict):
            raise ApiError(400, "Неверный JSON.")
        return data

    def _authorize(self) -> None:
        if not self.auth.check(self._token()):
            raise ApiError(401, "Нужно войти.")

    def _require_header(self) -> None:
        # Браузер не отправит этот заголовок с чужой страницы без CORS-разрешения,
        # а куки при этом не различают порты: их видят и сайты на этой же малине.
        if self.headers.get("X-Malinnik") != "1":
            raise ApiError(403, "Запрос отклонён.")

    # --- маршруты ---

    def _fail(self, status: int, message: str) -> None:
        """Отвечает ошибкой, даже если тело запроса, например архив, ещё не прочитано."""
        if self.body_read:
            self._json({"error": message}, status)
            return
        try:
            unread = int(self.headers.get("Content-Length", 0))
        except ValueError:
            unread = -1
        if unread == 0 and "Transfer-Encoding" not in self.headers:
            self._json({"error": message}, status)
            return
        # Остаток тела нельзя принять за следующий запрос, поэтому соединение закрываем.
        # Но при закрытии с непрочитанными данными клиент получает сброс и теряет наш ответ,
        # а значит, надо сначала ответить и дочитать ограниченный объём.
        self._json({"error": message}, status, {"Connection": "close"})
        self.connection.settimeout(2)
        left = min(max(unread, 0), MAX_DRAIN)
        try:
            while left > 0:
                chunk = self.rfile.read(min(CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    def _handle(self) -> None:
        self.body_read = False
        try:
            self._route(urlsplit(self.path))
        except (ApiError, SiteError) as exc:
            self._fail(exc.status, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.exception("Ошибка запроса %s %s", self.command, self.path)
            self.close_connection = True
            self._json({"error": "Внутренняя ошибка панели. Подробности в журнале сервиса."}, 500)

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = _handle

    def _route(self, url) -> None:
        method, path = self.command, url.path

        if method in ("GET", "HEAD") and path in WEB_FILES:
            name, content_type = WEB_FILES[path]
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                self._respond(200, f.read(), content_type)
            return

        if not path.startswith("/api/"):
            raise ApiError(404, "Не найдено.")
        if method not in ("GET", "HEAD"):
            self._require_header()

        if path == "/api/login" and method == "POST":
            self._login()
            return

        self._authorize()
        query = parse_qs(url.query)

        if path == "/api/logout" and method == "POST":
            self.auth.logout(self._token())
            self._json({"ok": True}, extra={"Set-Cookie": self._cookie("", 0)})
            return
        if path == "/api/state" and method == "GET":
            self._json(self.manager.state())
            return
        if path == "/api/upload" and method == "POST":
            self._upload(query)
            return
        if path.startswith("/api/telegram"):
            self._telegram(method, path)
            return
        if path == "/api/portfolio" and method == "POST":
            self.manager.update_portfolio(self._body())
            self._json({"ok": True})
            return

        match = SITE_ROUTE.match(path)
        if not match:
            raise ApiError(404, "Не найдено.")
        site_id, action = match.groups()

        if action == "cover":
            self._cover(method, site_id)
        elif action == "logs" and method == "GET":
            site = self.manager.get(site_id)
            sources = {"run": site.run_log, "deploy": site.deploy_log}
            source = sources.get(query.get("kind", ["run"])[0], site.run_log)
            self._json({"text": source.tail()})
        elif action is None and method == "PATCH":
            self.manager.update_settings(site_id, self._body())
            self._json({"ok": True})
        elif action is None and method == "DELETE":
            self.manager.delete(site_id)
            self._json({"ok": True})
        elif action and method == "POST":
            self.manager.action(site_id, action)
            self._json({"ok": True})
        else:
            raise ApiError(404, "Не найдено.")

    def _cover(self, method: str, site_id: str) -> None:
        if method == "GET":
            cover = self.manager.read_cover(site_id)
            if not cover:
                raise ApiError(404, "Обложки нет.")
            self._respond(200, cover[0], cover[1])
        elif method == "DELETE":
            self.manager.remove_cover(site_id)
            self._json({"ok": True})
        elif method == "POST":
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise ApiError(411, "Браузер не указал размер файла.") from None
            if length > MAX_COVER:
                raise ApiError(413, "Обложка должна быть не больше 5 МБ.")
            self.body_read = True
            self.manager.set_cover(site_id, self.rfile.read(length))
            self._json({"ok": True})
        else:
            raise ApiError(404, "Не найдено.")

    def _telegram(self, method: str, path: str) -> None:
        telegram = self.manager.telegram
        if method != "POST":
            raise ApiError(404, "Не найдено.")
        try:
            if path == "/api/telegram":
                telegram.update(self._body())
                self._json(telegram.to_json())
            elif path == "/api/telegram/find-chat":
                self._json({"chat": telegram.find_chat(), **telegram.to_json()})
            elif path == "/api/telegram/test":
                telegram.send_test()
                self._json({"ok": True})
            else:
                raise ApiError(404, "Не найдено.")
        except TelegramError as exc:
            raise ApiError(400 if exc.permanent else 502, str(exc)) from None

    def _cookie(self, value: str, max_age: int) -> str:
        cookie = f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
        # Куки не различают порты. Secure не даёт им уйти на сайты малины, работающие по HTTP.
        if self.headers.get("X-Forwarded-Proto") == "https":
            cookie += "; Secure"
        return cookie

    def _client_ip(self) -> str:
        """Адрес посетителя. За nginx все запросы идут с 127.0.0.1, а настоящий адрес в заголовке.

        Заголовку верим только от локального соединения: снаружи его может подделать кто угодно.
        """
        peer = self.client_address[0]
        forwarded = self.headers.get("X-Real-IP", "").strip()
        return forwarded if forwarded and peer in ("127.0.0.1", "::1") else peer

    def _login(self) -> None:
        ip = self._client_ip()
        wait = self.auth.locked_for(ip)
        data = self._body()
        if wait:
            self._json({"error": f"Слишком много попыток. Подожди {wait} с."}, 429)
            return
        token = self.auth.login(ip, str(data.get("password", "")))
        if not token:
            raise ApiError(401, "Неверный пароль.")
        self._json({"ok": True}, extra={"Set-Cookie": self._cookie(token, SESSION_TTL)})

    def _upload(self, query: dict) -> None:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Браузер не указал размер файла.") from None
        filename = query.get("filename", [""])[0]
        if not filename:
            raise ApiError(400, "Не указано имя файла.")
        free = shutil.disk_usage(self.manager.data_dir).free
        if length + DISK_RESERVE > free:
            raise ApiError(507, "На малине не хватает места для этого архива.")

        self.body_read = True
        fd, tmp_path = tempfile.mkstemp(dir=self.manager.tmp_dir, prefix="upload-")
        try:
            left = length
            with os.fdopen(fd, "wb") as out:
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            if left > 0:  # загрузку отменили или оборвалась связь
                self.close_connection = True
                return
            site = self.manager.receive_upload(tmp_path, filename, query.get("site", [None])[0])
            self._json({"ok": True, "id": site.id})
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        # Посетитель закрыл вкладку или пропала связь: обычное дело, журналу это не нужно.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def create_server(host: str, port: int, data_dir: str) -> tuple[ThreadingHTTPServer, Manager]:
    manager = Manager(data_dir)
    Bot(manager).attach()
    handler = type("BoundHandler", (Handler,), {"manager": manager, "auth": Auth(data_dir)})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    return server, manager
