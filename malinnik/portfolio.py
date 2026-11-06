"""Публичная страница портфолио.

Отдельный сервер на своём порту, а не маршрут панели: у него нет ни входа, ни действий,
поэтому через него нельзя добраться до API админки, даже если ошибиться в настройке прокси.
Отдаёт только то, что перечислено в Manager.public_site: без команд, переменных и журналов.
"""
from __future__ import annotations

import html
import logging
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

log = logging.getLogger("malinnik")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
ASSETS = {
    "/portfolio.css": ("portfolio.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
COVER_ROUTE = re.compile(r"^/covers/([a-z0-9-]+)$")
SAFE_HOST = re.compile(r"^[A-Za-z0-9.-]+$")
KINDS = {"node": "Node.js", "python": "Python", "php": "PHP", "custom": "Приложение"}
TONES = 6
MAX_DRAIN = 1024 * 1024
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def request_host(header: str | None) -> str:
    """Имя сервера из заголовка Host без порта. Заголовок задаёт клиент, поэтому проверяем его."""
    host = (header or "").rsplit(":", 1)[0] if ":" in (header or "") else (header or "")
    return host if SAFE_HOST.match(host) else "localhost"


def _tone(site_id: str) -> int:
    return sum(site_id.encode()) % TONES


def _card(site: dict) -> str:
    e = html.escape
    name = e(site["name"])
    if site["cover"]:
        visual = f'<img src="/covers/{e(site["id"])}?v={int(site["cover"])}" alt="" loading="lazy">'
    else:
        visual = f'<span class="tile tone-{_tone(site["id"])}" aria-hidden="true">{e(site["name"][:1].upper())}</span>'
    description = f'<p class="about">{e(site["description"])}</p>' if site["description"] else ""
    kind = KINDS.get(site["kind"], "")
    tag = f'<span class="tag">{e(kind)}</span>' if kind else ""
    if site["online"]:
        title = f'<h2><a href="{e(site["url"])}" target="_blank" rel="noopener noreferrer">{name}</a></h2>'
        status = '<span class="go">Открыть сайт <span aria-hidden="true">↗</span></span>'
        modifier = ""
    else:
        title = f"<h2>{name}</h2>"
        status = '<span class="off">Сейчас недоступен</span>'
        modifier = " is-off"
    return (
        f'<li class="card{modifier}"><div class="visual">{visual}</div>'
        f'<div class="body">{tag}{title}{description}{status}</div></li>'
    )


def render_page(settings: dict, sites: list[dict]) -> str:
    e = html.escape
    title = e(settings["title"])
    subtitle = f'<p class="subtitle">{e(settings["subtitle"])}</p>' if settings["subtitle"] else ""
    if sites:
        content = f'<ul class="grid">{"".join(_card(site) for site in sites)}</ul>'
    else:
        content = '<p class="empty">Здесь скоро появятся работы.</p>'
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{e(settings["subtitle"] or settings["title"])}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/portfolio.css">
</head>
<body>
<header class="hero"><h1>{title}</h1>{subtitle}</header>
<main>{content}</main>
</body>
</html>
"""


class PublicHandler(BaseHTTPRequestHandler):
    server_version = "Aggentnik"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30
    manager = None

    def log_message(self, format, *args):
        log.debug("portfolio %s", format % args)

    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-cache") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _not_found(self) -> None:
        self._send(404, "Страница не найдена".encode("utf-8"), "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/":
                host = request_host(self.headers.get("Host"))
                state = self.manager.public_state(host)
                body = render_page(state["settings"], state["sites"]).encode("utf-8")
                self._send(200, body, "text/html; charset=utf-8")
            elif path in ASSETS:
                name, content_type = ASSETS[path]
                with open(os.path.join(WEB_DIR, name), "rb") as f:
                    self._send(200, f.read(), content_type, "public, max-age=3600")
            else:
                match = COVER_ROUTE.match(path)
                cover = self.manager.public_cover(match.group(1)) if match else None
                if cover:
                    self._send(200, cover[0], cover[1], "public, max-age=86400")
                else:
                    self._not_found()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001
            log.exception("Ошибка страницы портфолио %s", self.path)
            self.close_connection = True
            self._send(500, "Ошибка сервера".encode("utf-8"), "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def _read_only(self) -> None:
        # Если закрыть соединение с непрочитанным телом, клиент получит сброс вместо ответа.
        try:
            unread = min(max(int(self.headers.get("Content-Length", 0)), 0), MAX_DRAIN)
        except ValueError:
            unread = 0
        if unread:
            self.connection.settimeout(2)
            try:
                self.rfile.read(unread)
            except OSError:
                pass
        self.close_connection = True
        self._send(405, "Только чтение".encode("utf-8"), "text/plain; charset=utf-8")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _read_only


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


def create_public_server(host: str, port: int, manager) -> ThreadingHTTPServer:
    handler = type("BoundPublicHandler", (PublicHandler,), {"manager": manager})
    return _Server((host, port), handler)
