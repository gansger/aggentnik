"""Запуск: python3 -m malinnik serve | set-password"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import threading

from . import auth
from .portfolio import create_public_server
from .server import create_server


def main() -> None:
    parser = argparse.ArgumentParser(prog="malinnik")
    parser.add_argument("--data", default=os.environ.get("MALINNIK_DATA", "data"), help="папка с данными")
    commands = parser.add_subparsers(dest="command")

    serve = commands.add_parser("serve", help="запустить панель")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=7700)
    serve.add_argument("--public-host", default="127.0.0.1", help="адрес публичной страницы портфолио")
    serve.add_argument("--public-port", type=int, default=0, help="порт портфолио, 0 — не запускать")

    password = commands.add_parser("set-password", help="задать пароль панели")
    password.add_argument("--generate", action="store_true", help="создать случайный и показать его")

    args = parser.parse_args()
    data_dir = os.path.abspath(args.data)

    if args.command == "set-password":
        if args.generate:
            value = auth.generate_password()
            print(value)
        else:
            value = getpass.getpass("Новый пароль: ")
            if not value or value != getpass.getpass("Ещё раз: "):
                sys.exit("Пароли пустые или не совпадают.")
        auth.set_password(data_dir, value)
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 7700)

    if not auth.has_password(data_dir):
        value = auth.generate_password()
        auth.set_password(data_dir, value)
        print(f"Создан пароль панели: {value}", flush=True)

    server, manager = create_server(host, port, data_dir)
    public = None
    public_port = getattr(args, "public_port", 0)
    if public_port:
        public = create_public_server(getattr(args, "public_host", "127.0.0.1"), public_port, manager)
        manager.public_port = public_port
        threading.Thread(target=public.serve_forever, daemon=True).start()
        logging.info("Портфолио запущено на порту %s", public_port)

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
        if public:
            threading.Thread(target=public.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    manager.run()
    logging.info("Панель запущена на порту %s", port)
    try:
        server.serve_forever()
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
