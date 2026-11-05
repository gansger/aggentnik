"""Определяет тип сайта и то, как его установить и запустить.

Команды хранятся строками для shell. `$PORT` панель подставляет сама перед запуском,
поэтому одна и та же команда работает и в sh, и в cmd.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable

STATIC_SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_server.py")
PHP_ROUTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "php_router.php")
SERVER_DEPS = {
    "express", "koa", "fastify", "@hapi/hapi", "hapi", "restify", "polka", "hono",
    "@nestjs/core", "socket.io", "ws", "sails", "nuxt",
}
# Такой "start" поднимает dev-сервер, а не готовый сайт: раздаём результат сборки.
DEV_SERVER = re.compile(
    r"react-scripts\s+start|\bvite\b|ng\s+serve|vue-cli-service\s+serve"
    r"|webpack-dev-server|webpack\s+serve|parcel(?!\s+build)|live-server"
)
PY_ENTRIES = ("app.py", "main.py", "server.py", "run.py", "wsgi.py", "asgi.py", "application.py")
WINDOWS_ONLY_REQS = re.compile(r"^\s*(pywin32|pypiwin32|pywin32-ctypes|pywinpty)\b", re.I)
STATIC_DIRS = (".", "dist", "build", "out", "public", "www", "site", "docs", "htdocs", "html")


class DetectError(Exception):
    """Сообщение этой ошибки показывается пользователю."""


@dataclass
class Plan:
    kind: str
    start: str = ""
    steps: list[tuple[str, str]] = field(default_factory=list)  # (этап, команда)
    finish: Callable[[], tuple[str, str]] | None = None  # решает, что запускать, после сборки
    override: str = ""

    def resolve(self) -> None:
        """Вызывается после установки и сборки: фиксирует итоговую команду запуска."""
        if self.finish:
            try:
                self.kind, self.start = self.finish()
            except DetectError:
                if not self.override:
                    raise
        if self.override:
            self.start = self.override


def q(value: str) -> str:
    return shlex.quote(value) if os.name == "posix" else f'"{value}"'


def _find_index(folder: str) -> str | None:
    """Имя главной страницы. На Windows Index.html и index.html — один файл, на Linux нет."""
    if not os.path.isdir(folder):
        return None
    pages = [n for n in os.listdir(folder) if n.lower().endswith((".html", ".htm"))
             and os.path.isfile(os.path.join(folder, n))]
    for page in pages:
        if page.lower() in ("index.html", "index.htm"):
            return page
    return None


def _static_command(root: str, index: str) -> str:
    return f"{q(sys.executable)} {q(STATIC_SERVER)} --port $PORT --root {q(root)} --index {q(index)}"


def _find_static(app: str, dirs=STATIC_DIRS) -> tuple[str, str] | None:
    for name in dirs:
        folder = os.path.normpath(os.path.join(app, name))
        index = _find_index(folder)
        if index:
            return folder, index
    for name in sorted(os.listdir(app)):
        folder = os.path.join(app, name)
        if os.path.isdir(folder) and not name.startswith("."):
            index = _find_index(folder)
            if index:
                return folder, index
    pages = [n for n in os.listdir(app) if n.lower().endswith((".html", ".htm"))]
    if len(pages) == 1:  # единственная страница с любым именем: main.html, Сайт.html
        return app, pages[0]
    return None


def _read_package(app: str) -> dict | None:
    path = os.path.join(app, "package.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise DetectError(f"Не удалось прочитать package.json: {exc}") from exc
    return data if isinstance(data, dict) else None


def _node_entry(app: str, pkg: dict) -> str | None:
    main = pkg.get("main")
    candidates = [main] if isinstance(main, str) else []
    candidates += ["server.js", "app.js", "index.js", "server.mjs", "index.mjs",
                   "src/server.js", "src/index.js", "server/index.js"]
    for rel in candidates:
        if os.path.isfile(os.path.join(app, rel)):
            return rel
    return None


def _node_finish(app: str, pkg: dict) -> tuple[str, str]:
    scripts = pkg.get("scripts") or {}
    deps = {**(pkg.get("devDependencies") or {}), **(pkg.get("dependencies") or {})}
    start = scripts.get("start", "")

    if "next" in deps:
        out = os.path.join(app, "out")
        index = _find_index(out)
        if index:  # output: "export"
            return "static", _static_command(out, index)
        return "node", "npx next start -H 0.0.0.0 -p $PORT"
    if start and not DEV_SERVER.search(start):
        return "node", "npm start"
    for name in ("dist", "build", "out"):
        folder = os.path.join(app, name)
        index = _find_index(folder)
        if index:
            return "static", _static_command(folder, index)
    entry = _node_entry(app, pkg)
    if entry and SERVER_DEPS.intersection(deps):
        return "node", f"node {q(entry)}"
    static = _find_static(app, (".", "public"))
    if static:
        return "static", _static_command(*static)
    if entry:
        return "node", f"node {q(entry)}"
    raise DetectError(
        "В package.json нет команды start и не найден ни server.js, ни папка сборки. "
        "Укажи команду запуска в настройках сайта."
    )


def _plan_node(app: str, pkg: dict) -> Plan:
    if not shutil.which("npm"):
        raise DetectError("Это Node.js-сайт, но на малине не установлен npm: sudo apt install nodejs npm")
    steps = [("install", "npm install --no-audit --no-fund")]
    if (pkg.get("scripts") or {}).get("build"):
        steps.append(("build", "npm run build"))
    return Plan("node", steps=steps, finish=lambda: _node_finish(app, pkg))


def _clean_requirements(path: str) -> None:
    """Убирает пакеты только для Windows и чинит UTF-16 после `pip freeze >` в PowerShell."""
    with open(path, "rb") as f:
        raw = f.read()
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    lines = raw.decode(encoding, "replace").splitlines()
    kept = [line for line in lines if os.name != "posix" or not WINDOWS_ONLY_REQS.match(line)]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(kept) + "\n")


def _python_framework(app: str) -> tuple[str, str, str] | None:
    """Ищет `app = Flask(...)` или `app = FastAPI(...)`. Вернёт (фреймворк, модуль, переменная)."""
    pattern = re.compile(r"^\s*(\w+)\s*(?::\s*\w+\s*)?=\s*(?:\w+\.)?(Flask|FastAPI)\(", re.M)
    for name in PY_ENTRIES:
        path = os.path.join(app, name)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            match = pattern.search(f.read())
        if match:
            return match.group(2).lower(), name[:-3], match.group(1)
    return None


def _plan_python(app: str) -> Plan | None:
    has_manage = os.path.isfile(os.path.join(app, "manage.py"))
    entry = next((n for n in PY_ENTRIES if os.path.isfile(os.path.join(app, n))), None)
    if not has_manage and not entry:
        return None

    bin_dir = "bin" if os.name == "posix" else "Scripts"
    py_name = "python" if os.name == "posix" else "python.exe"
    python = q(os.path.join(app, ".venv", bin_dir, py_name))
    steps = [("install", f"{q(sys.executable)} -m venv .venv")]

    pip = f"{python} -m pip install --disable-pip-version-check"
    requirements = os.path.join(app, "requirements.txt")
    if os.path.isfile(requirements):
        _clean_requirements(requirements)
        steps.append(("install", f"{pip} -r requirements.txt"))
    elif os.path.isfile(os.path.join(app, "pyproject.toml")):
        steps.append(("install", f"{pip} ."))

    if has_manage:
        steps.append(("build", f"{python} manage.py migrate --noinput"))
        return Plan("python", steps=steps, start=f"{python} manage.py runserver 0.0.0.0:$PORT --noreload")

    framework = _python_framework(app)
    if framework is None:
        return Plan("python", steps=steps, start=f"{python} {q(entry)}")
    name, module, variable = framework
    if name == "fastapi":
        steps.append(("install", f"{pip} uvicorn"))
        start = f"{python} -m uvicorn {module}:{variable} --host 0.0.0.0 --port $PORT"
    else:
        steps.append(("install", f"{pip} waitress"))
        start = f"{python} -m waitress --listen=0.0.0.0:$PORT {module}:{variable}"
    return Plan("python", steps=steps, start=start)


def _plan_php(app: str) -> Plan | None:
    public_index = os.path.isfile(os.path.join(app, "public", "index.php"))
    root_php = any(n.lower().endswith(".php") for n in os.listdir(app))
    if not public_index and not root_php:
        return None
    if not shutil.which("php"):
        raise DetectError("Это PHP-сайт, но на малине не установлен PHP: sudo apt install php-cli")
    steps = []
    if os.path.isfile(os.path.join(app, "composer.json")) and shutil.which("composer"):
        steps.append(("install", "composer install --no-interaction --no-dev"))
    docroot = "public" if public_index else "."
    return Plan("php", steps=steps, start=f"php -S 0.0.0.0:$PORT -t {docroot} {q(PHP_ROUTER)}")


def _auto(app: str) -> Plan:
    if os.path.isfile(os.path.join(app, "start.sh")):
        return Plan("custom", start="bash start.sh")

    php = _plan_php(app)
    if php:
        return php

    pkg = _read_package(app)
    python = _plan_python(app)
    # package.json рядом с Python-кодом обычно нужен только для сборки стилей.
    if pkg is not None and (python is None or (pkg.get("scripts") or {}).get("start")):
        return _plan_node(app, pkg)
    if python:
        return python

    static = _find_static(app)
    if static:
        return Plan("static", start=_static_command(*static))

    seen = ", ".join(sorted(os.listdir(app))[:8])
    raise DetectError(
        f"Не понял, что это за сайт. В архиве: {seen}. "
        "Нужен index.html, package.json, app.py, manage.py или index.php. "
        "Либо укажи команду запуска в настройках сайта и нажми «Пересобрать»."
    )


def detect(app: str, override: str = "") -> Plan:
    """Строит план для распакованного сайта. override — команда запуска от пользователя."""
    try:
        plan = _auto(app)
    except DetectError:
        if not override:
            raise
        plan = Plan("custom")
    plan.override = override
    return plan
