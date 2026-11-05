"""Раздаёт статический сайт. Запускается панелью отдельным процессом на порту сайта."""
from __future__ import annotations

import argparse
import functools
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit


class Handler(SimpleHTTPRequestHandler):
    index = "index.html"
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".wasm": "application/wasm",
        ".webp": "image/webp",
    }

    def send_head(self):
        path = unquote(urlsplit(self.path).path)
        segments = [s for s in path.split("/") if s]
        hidden = any(s.startswith(".") and s != ".well-known" for s in segments)
        if hidden or path.lower().endswith((".db", ".sqlite", ".sqlite3")):
            self.send_error(404)  # .env, .git и базы данных наружу не отдаём
            return None

        # translate_path снова раскодирует путь, поэтому подменяем его закодированным.
        home = "/" + quote(self.index)
        full = self.translate_path(self.path)
        if os.path.isdir(full):
            if path.endswith("/") and not os.path.exists(os.path.join(full, "index.html")):
                self.path = home
        elif not os.path.exists(full):
            if os.path.isfile(full + ".html"):
                self.path = quote(path + ".html")  # /about → about.html
            elif not os.path.splitext(path)[1]:
                self.path = home  # маршруты одностраничных приложений
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, format, *args):
        if self.headers and "malinnik-health" in self.headers.get("User-Agent", ""):
            return
        super().log_message(format, *args)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--index", default="index.html")
    args = parser.parse_args()

    Handler.index = args.index
    handler = functools.partial(Handler, directory=args.root)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), handler)
    print(f"Статический сайт на порту {args.port}: {args.root}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
