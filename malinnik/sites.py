"""Сайты: хранение, установка из архива, запуск и присмотр за процессами."""
from __future__ import annotations

import fnmatch
import http.client
import json
import logging
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import threading
import time
from urllib.parse import urlsplit

from . import archive, detect, metrics
from .alerts import Alerts
from .procs import LogFile
from .procs import kill as _kill
from .procs import spawn as _spawn
from .telegram import Telegram

log = logging.getLogger("malinnik")

PORT_RANGE = range(8100, 8200)
MAX_DESCRIPTION = 500
DISK_RESERVE = 200 * 1024 * 1024
READY_TIMEOUT = 45
STEP_TIMEOUT = 45 * 60
MAX_FAST_CRASHES = 5
STABLE_AFTER = 60
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ARCHIVE_SUFFIX = re.compile(r"\.(zip|7z|rar|tgz|tbz2?|txz|tar(\.(gz|bz2|xz))?)$", re.I)
PERSIST_FILES = ("*.db", "*.sqlite", "*.sqlite3", ".env")
PERSIST_DIRS = {"uploads", "media", "data", "storage"}
SKIP_DIRS = {"node_modules", ".venv", "__pycache__", ".git"}
TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


class SiteError(Exception):
    """Ошибка действия с сайтом. status — код HTTP-ответа."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class StepFailed(Exception):
    pass


def site_name(filename: str) -> str:
    base = os.path.basename(filename.replace("\\", "/"))
    return ARCHIVE_SUFFIX.sub("", base).strip() or "site"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower().translate(TRANSLIT)).strip("-")
    return slug[:40].strip("-") or "site"


def parse_env(text: str) -> dict[str, str]:
    env = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not ENV_KEY.match(key):
            raise SiteError(f"Строка {number}: нужен формат ИМЯ=значение")
        env[key] = value.strip()
    return env


def cover_type(data: bytes) -> str | None:
    """Тип картинки по её содержимому. SVG нельзя: в нём может быть скрипт."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _clean_public_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    # Ссылка попадает на публичную страницу: схема javascript: там недопустима.
    try:
        parts = urlsplit(value)
    except ValueError:
        parts = None
    if not parts or parts.scheme not in ("http", "https") or not parts.netloc \
            or len(value) > 300 or any(c.isspace() for c in value):
        raise SiteError("Адрес должен начинаться с http:// или https://.")
    return value


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _rmtree(path: str) -> None:
    def force(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=force)


def _port_open(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


class Site:
    def __init__(self, manager: "Manager", meta: dict):
        self.manager = manager
        self.meta = meta
        self.dir = os.path.join(manager.sites_dir, meta["id"])
        self.releases_dir = os.path.join(self.dir, "releases")
        self.data_dir = os.path.join(self.dir, "data")
        self.archive_path = os.path.join(self.dir, "last-archive")
        self.cover_path = os.path.join(self.dir, "cover")
        logs = os.path.join(self.dir, "logs")
        for folder in (self.releases_dir, self.data_dir, logs):
            os.makedirs(folder, exist_ok=True)
        self.run_log = LogFile(os.path.join(logs, "run.log"))
        self.deploy_log = LogFile(os.path.join(logs, "deploy.log"))

        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.restart_at: float | None = None
        self.crashes = 0
        self.restarts = 0
        self.exit_code: int | None = None
        self.gave_up = False
        self.deploying = False  # пока идёт установка, автоперезапуск не вмешивается
        self.deploy = {"active": False, "queued": False, "stage": None}
        self.stats: dict = {}
        self.health = {"tcp": False, "code": None, "ms": None}
        self.last_http = 0.0

    # --- данные ---

    @property
    def id(self) -> str:
        return self.meta["id"]

    def save(self) -> None:
        path = os.path.join(self.dir, "site.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.meta, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def release(self, number=None) -> dict | None:
        number = self.meta.get("current") if number is None else number
        return self.meta["releases"].get(str(number)) if number is not None else None

    def release_dir(self, number) -> str:
        return os.path.join(self.releases_dir, str(number))

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("MALINNIK_")}
        env.update({
            "PORT": str(self.meta["port"]),
            "HOST": "0.0.0.0",
            "DATA_DIR": self.data_dir,
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",  # журналы и файлы сайта в UTF-8 независимо от локали
            "npm_config_update_notifier": "false",
        })
        env.setdefault("LANG", "C.UTF-8")
        env.update(parse_env(self.meta.get("env", "")))
        return env

    # --- процесс ---

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            self.run_log.write(line)
        proc.stdout.close()

    def start(self) -> subprocess.Popen | None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return self.proc
            release = self.release()
            if not release:
                raise SiteError("Сайт ещё не установлен. Нажми «Пересобрать» или загрузи архив.", 409)
            port = str(self.meta["port"])
            command = self.meta.get("start_override") or release["start_cmd"]
            command = command.replace("${PORT}", port).replace("$PORT", port)
            env = self._env()
            if release["kind"] in ("node", "custom"):
                env.setdefault("NODE_ENV", "production")
            self.run_log.line(f"\n[аггентник] запуск: {command}")
            self.proc = _spawn(command, self.release_dir(self.meta["current"]), env)
            self.started_at = time.time()
            self.restart_at = None
            self.gave_up = False
            self.exit_code = None
            self.health = {"tcp": False, "code": None, "ms": None}
            self.last_http = 0.0
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            return self.proc

    def _stop_proc(self) -> None:
        with self.lock:
            proc, self.proc = self.proc, None
            self.restart_at = None
            self.stats = {}
            self.health = {"tcp": False, "code": None, "ms": None}
            if proc:
                _kill(proc)
                self.run_log.line("[аггентник] остановлен")

    def stop(self) -> None:
        self.meta["desired"] = "stopped"
        self.save()
        self.crashes = 0
        self.gave_up = False
        self._stop_proc()

    def launch(self) -> None:
        self.meta["desired"] = "running"
        self.save()
        self.crashes = 0
        self.start()

    def restart(self) -> None:
        self._stop_proc()
        self.launch()

    def watch(self) -> None:
        """Вызывается раз в секунду: замечает падения и перезапускает сайт."""
        with self.lock:
            now = time.time()
            if self.deploying:
                return
            if self.proc is None:
                if self.restart_at and now >= self.restart_at and self.meta["desired"] == "running":
                    self.restarts += 1
                    self.start()
                return
            code = self.proc.poll()
            if code is None:
                return
            ran = now - (self.started_at or now)
            self.proc = None
            self.exit_code = code
            self.stats = {}
            self.health = {"tcp": False, "code": None, "ms": None}
            self.run_log.line(f"[аггентник] процесс завершился с кодом {code}")
            if self.meta["desired"] != "running":
                return
            self.crashes = 1 if ran > STABLE_AFTER else self.crashes + 1
            if self.crashes > MAX_FAST_CRASHES:
                self.gave_up = True
                self.run_log.line("[аггентник] слишком много падений подряд, перезапуск отключён")
            else:
                self.restart_at = now + min(60, 2 ** self.crashes)

    def effective_port(self) -> int:
        """Порт сайта на деле: код может игнорировать PORT и слушать, например, 3000."""
        listen = self.stats.get("listen") or []
        return self.meta["port"] if not listen or self.meta["port"] in listen else listen[0]

    def check_health(self) -> None:
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        port = self.effective_port()
        tcp = _port_open(port)
        health = dict(self.health, tcp=tcp)
        if not tcp:
            health.update(code=None, ms=None)
        elif time.time() - self.last_http > 30 or self.health["code"] is None:
            # Сам запрос реже: иначе он забивает журнал доступа сайта.
            self.last_http = time.time()
            started = time.monotonic()
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/", headers={"User-Agent": "malinnik-health"})
                health["code"] = conn.getresponse().status
                health["ms"] = round((time.monotonic() - started) * 1000)
                conn.close()
            except (OSError, http.client.HTTPException):
                health.update(code=None, ms=None)
        if self.proc is proc:
            self.health = health

    def state(self) -> str:
        if self.deploy["active"]:
            return "deploying"
        proc = self.proc
        if proc and proc.poll() is None:
            if self.health["tcp"]:
                return "running"
            return "starting" if time.time() - (self.started_at or 0) < 60 else "unresponsive"
        if not self.release():
            return "failed"
        if self.meta["desired"] != "running":
            return "stopped"
        return "crashed" if self.gave_up else "restarting"

    def to_json(self) -> dict:
        release = self.release() or {}
        proc = self.proc
        alive = bool(proc and proc.poll() is None)
        return {
            "id": self.id,
            "name": self.meta["name"],
            "state": self.state(),
            "desired": self.meta["desired"],
            "kind": release.get("kind"),
            "port": self.meta["port"],
            "effective_port": self.effective_port(),
            "release": self.meta.get("current"),
            "previous": self.meta.get("previous"),
            "deployed_at": release.get("deployed_at"),
            "archive_name": release.get("archive_name"),
            "size": release.get("size"),
            "start_cmd": release.get("start_cmd"),
            "start_override": self.meta.get("start_override", ""),
            "env": self.meta.get("env", ""),
            "uptime": int(time.time() - self.started_at) if alive and self.started_at else None,
            "restarts": self.restarts,
            "exit_code": self.exit_code,
            "health": self.health,
            "cpu": self.stats.get("cpu"),
            "rss": self.stats.get("rss"),
            "deploy": self.deploy,
            "error": self.meta.get("last_error"),
            "warning": self.meta.get("last_warning"),
            "can_redeploy": os.path.exists(self.archive_path),
            "show_in_portfolio": self.meta.get("show_in_portfolio", True),
            "description": self.meta.get("description", ""),
            "public_url": self.meta.get("public_url", ""),
            "cover_version": self.meta.get("cover_version") or 0,
        }

    # --- установка ---

    def _run_step(self, command: str, cwd: str, env: dict) -> None:
        self.deploy_log.line(f"\n$ {command}")
        proc = _spawn(command, cwd, env)
        timer = threading.Timer(STEP_TIMEOUT, _kill, [proc])
        timer.start()
        try:
            for line in proc.stdout:
                self.deploy_log.write(line)
            code = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
        if code != 0:
            raise StepFailed(f"Команда завершилась с кодом {code}: {command}")

    def _run_steps(self, plan: detect.Plan, cwd: str, env: dict) -> None:
        for stage, command in plan.steps:
            self.deploy["stage"] = stage
            self._run_step(command, cwd, env)

    def _carry_over(self, old: str, new: str) -> None:
        """Базы и загрузки живут в папке сайта, поэтому при обновлении их не должно терять."""
        count = 0
        for root, dirs, files in os.walk(old):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            rel_root = os.path.relpath(root, old)
            parts = set(rel_root.split(os.sep))
            for name in files:
                keep = parts & PERSIST_DIRS or any(fnmatch.fnmatch(name, p) for p in PERSIST_FILES)
                target = os.path.join(new, rel_root, name)
                if keep and not os.path.exists(target):
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(os.path.join(root, name), target)
                    count += 1
        if count:
            self.deploy_log.line(f"Из прошлой версии перенесено файлов: {count}")

    def _wait_ready(self, proc: subprocess.Popen) -> str:
        """'up' — порт открыт, 'dead' — процесс упал, 'slow' — работает, но порт пока закрыт."""
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if proc.poll() is not None:
                return "dead"
            if _port_open(self.effective_port(), 0.5):
                # Порт уже открыт, но приложение может упасть сразу после старта.
                time.sleep(1)
                return "dead" if proc.poll() is not None else "up"
            self.manager.sample_one(self)
            time.sleep(0.5)
        return "slow"

    def _prune_releases(self) -> None:
        keep = {str(self.meta.get("current")), str(self.meta.get("previous"))}
        self.meta["releases"] = {k: v for k, v in self.meta["releases"].items() if k in keep}
        for name in os.listdir(self.releases_dir):
            if name not in keep:
                _rmtree(os.path.join(self.releases_dir, name))

    def run_deploy(self, archive_name: str) -> None:
        self.deploy.update(active=True, queued=False, stage="extract")
        self.meta["last_error"] = None
        self.meta["last_warning"] = None
        self.deploy_log.clear()
        number = self.meta["next_release"]
        self.meta["next_release"] = number + 1
        self.save()
        target = self.release_dir(number)
        old_number = self.meta.get("current")
        switched = False
        try:
            self.deploy_log.line(f"Распаковка {archive_name}")
            limit = shutil.disk_usage(self.dir).free - DISK_RESERVE
            archive.extract(self.archive_path, target, limit)

            self.deploy["stage"] = "detect"
            plan = detect.detect(target, self.meta.get("start_override", ""))
            env = self._env()
            if "NODE_ENV" not in parse_env(self.meta.get("env", "")):
                env.pop("NODE_ENV", None)  # при NODE_ENV=production npm не поставит пакеты для сборки
            self.deploy_log.line(f"Тип сайта: {plan.kind}")
            try:
                self._run_steps(plan, target, env)
            except StepFailed:
                lock = os.path.join(target, "package-lock.json")
                if plan.kind != "node" or not os.path.exists(lock):
                    raise
                # Lock-файл из Windows часто не содержит сборок пакетов под ARM Linux.
                self.deploy_log.line("\nНе вышло. Пробую ещё раз без package-lock.json из Windows.")
                os.unlink(lock)
                shutil.rmtree(os.path.join(target, "node_modules"), ignore_errors=True)
                self._run_steps(plan, target, env)
            plan.resolve()
            if not plan.start:
                raise detect.DetectError("Не задана команда запуска.")

            self.deploy["stage"] = "start"
            self.deploy_log.line(f"Команда запуска: {plan.start}")
            self.deploying = True
            self._stop_proc()
            if old_number is not None:
                self._carry_over(self.release_dir(old_number), target)
            self.meta["releases"][str(number)] = {
                "kind": plan.kind,
                "start_cmd": plan.start,
                "archive_name": archive_name,
                "deployed_at": int(time.time()),
                "size": _dir_size(target),
            }
            self.meta["current"] = number
            self.meta["previous"] = old_number
            self.meta["desired"] = "running"
            switched = True
            self.save()
            self.crashes = 0
            proc = self.start()
            result = self._wait_ready(proc)
            if result == "dead":
                self.deploy_log.line("\nСайт упал сразу после запуска. Последние строки его журнала:")
                self.deploy_log.line(self.run_log.tail(25))
                raise StepFailed(f"Сайт упал при запуске (код {proc.returncode}).")
            if result == "slow":
                self.meta["last_warning"] = (
                    f"Процесс работает, но за {READY_TIMEOUT} секунд не открыл порт. "
                    "Возможно, сайт долго запускается: посмотри журнал."
                )
            self.check_health()  # чтобы статус сразу стал «Работает», а не ждал следующего замера
            self._prune_releases()
            self.save()
            self.deploy_log.line("\nГотово.")
            self.manager.notify(f"«{self.meta['name']}» установлен, версия {number}.")
        except (archive.ArchiveError, detect.DetectError, StepFailed, SiteError) as exc:
            self._deploy_failed(str(exc), number, old_number, switched)
        except Exception as exc:  # noqa: BLE001 — падение одной установки не должно ронять очередь
            log.exception("Ошибка установки сайта %s", self.id)
            self._deploy_failed(f"Внутренняя ошибка: {exc}", number, old_number, switched)
        finally:
            self.deploying = False
            self.deploy.update(active=False, queued=False, stage=None)
            _rmtree_quiet(target + ".unwrap")

    def _deploy_failed(self, message: str, number: int, old_number, switched: bool) -> None:
        self.deploy_log.line(f"\nОшибка: {message}")
        if switched:
            self._stop_proc()
            self.meta["current"] = old_number
            self.meta["previous"] = None
            self.meta["releases"].pop(str(number), None)
            if old_number is not None:
                message += " Вернул прошлую версию."
                self.deploying = False
                try:
                    self.start()
                except SiteError:
                    pass
        self.meta["last_error"] = message
        self.save()
        _rmtree_quiet(self.release_dir(number))
        self.manager.notify(f"«{self.meta['name']}»: установка не удалась. {message}")

    def rollback(self) -> None:
        previous = self.meta.get("previous")
        if previous is None:
            raise SiteError("Прошлой версии нет.", 409)
        self._stop_proc()
        self.meta["previous"], self.meta["current"] = self.meta["current"], previous
        self.meta["last_error"] = None
        self.meta["last_warning"] = None
        self.launch()


def _rmtree_quiet(path: str) -> None:
    if os.path.isdir(path):
        try:
            _rmtree(path)
        except OSError:
            log.warning("Не удалось удалить %s", path)


class Manager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.sites_dir = os.path.join(data_dir, "sites")
        self.tmp_dir = os.path.join(data_dir, "tmp")
        os.makedirs(self.sites_dir, exist_ok=True)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        os.makedirs(self.tmp_dir, exist_ok=True)
        self.sites: dict[str, Site] = {}
        self.lock = threading.RLock()
        self.jobs: queue.Queue = queue.Queue()
        self.closing = threading.Event()
        self.sampler = metrics.Sampler()
        self.system = self.sampler.system(self.data_dir)
        self.telegram = Telegram(data_dir)
        self.alerts = Alerts(self.notify)
        self.portfolio_path = os.path.join(data_dir, "portfolio.json")
        self.portfolio = self._load_portfolio()
        self.public_port = 0  # порт публичной страницы, его задаёт __main__
        self._load()

    def notify(self, text: str) -> None:
        self.telegram.notify(text)

    def _load(self) -> None:
        for name in os.listdir(self.sites_dir):
            path = os.path.join(self.sites_dir, name, "site.json")
            try:
                with open(path, encoding="utf-8") as f:
                    site = Site(self, json.load(f))
            except (OSError, ValueError) as exc:
                log.warning("Пропускаю сайт %s: %s", name, exc)
                continue
            self.sites[site.id] = site
            site._prune_releases()  # убирает недоделанную установку после сбоя питания

    def run(self) -> None:
        """Запускает сохранённые сайты и фоновые потоки."""
        for site in self.sites.values():
            if site.meta["desired"] == "running" and site.release():
                try:
                    site.start()
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось запустить сайт %s", site.id)
        self.telegram.run()
        count = sum(1 for s in self.sites.values() if s.proc)
        self.notify(f"Аггентник запущен, сайтов работает: {count}.")
        for target in (self._deploy_worker, self._watch_loop, self._metrics_loop):
            threading.Thread(target=target, daemon=True).start()

    def shutdown(self) -> None:
        self.closing.set()
        self.telegram.shutdown()
        sites = list(self.sites.values())
        threads = [threading.Thread(target=s._stop_proc) for s in sites]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

    # --- фоновые потоки ---

    def _deploy_worker(self) -> None:
        # Сборки идут строго по одной: параллельно малине не хватит памяти.
        while True:
            site, archive_name = self.jobs.get()
            site.run_deploy(archive_name)
            self.jobs.task_done()

    def _watch_loop(self) -> None:
        while not self.closing.wait(1):
            for site in list(self.sites.values()):
                try:
                    site.watch()
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка присмотра за сайтом %s", site.id)

    def sample_one(self, site: Site) -> None:
        proc = site.proc
        if proc and metrics.HAS_PROC:
            sampled = self.sampler.sites({proc.pid} | self._sessions())
            if proc.pid in sampled:
                site.stats = sampled[proc.pid]

    def _sessions(self) -> set[int]:
        procs = [s.proc for s in list(self.sites.values())]  # s.proc может обнулиться в другом потоке
        return {p.pid for p in procs if p}

    def _metrics_loop(self) -> None:
        while not self.closing.wait(3):
            try:
                sampled = self.sampler.sites(self._sessions())
                for site in list(self.sites.values()):
                    proc = site.proc
                    if proc and proc.pid in sampled:
                        site.stats = sampled[proc.pid]
                    site.check_health()
                self.system = self.sampler.system(self.data_dir)
                sites = [s.to_json() for s in list(self.sites.values())]
                self.alerts.check(sites, self.system)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка сбора показателей")

    # --- действия ---

    def get(self, site_id: str) -> Site:
        site = self.sites.get(site_id)
        if not site:
            raise SiteError("Такого сайта нет.", 404)
        return site

    def _free_port(self) -> int:
        taken = {s.meta["port"] for s in list(self.sites.values())}
        for port in PORT_RANGE:
            # Одной пробной привязки мало: на Windows она удаётся и на занятом порту.
            if port in taken or _port_open(port, 0.2):
                continue
            with socket.socket() as sock:
                try:
                    sock.bind(("0.0.0.0", port))
                    return port
                except OSError:
                    continue
        raise SiteError("Свободные порты закончились.", 507)

    def _create(self, name: str) -> Site:
        base = slugify(name)
        site_id, n = base, 2
        while site_id in self.sites:
            site_id, n = f"{base}-{n}", n + 1
        site = Site(self, {
            "id": site_id, "name": name, "port": self._free_port(), "desired": "running",
            "current": None, "previous": None, "next_release": 1, "releases": {},
            "env": "", "start_override": "", "created_at": int(time.time()),
        })
        site.save()
        self.sites[site_id] = site
        return site

    def _enqueue(self, site: Site, archive_name: str) -> None:
        site.deploy.update(active=True, queued=True, stage=None)
        site.meta["last_archive_name"] = archive_name
        site.save()
        self.jobs.put((site, archive_name))

    def receive_upload(self, tmp_path: str, filename: str, site_id: str | None) -> Site:
        """Принимает загруженный архив: обновляет сайт или заводит новый."""
        name = site_name(filename)
        with self.lock:
            if site_id:
                site = self.get(site_id)
            else:
                existing = list(self.sites.values())
                site = next((s for s in existing if s.meta["name"].lower() == name.lower()), None)
                site = site or self._create(name)
            if site.deploy["active"]:
                raise SiteError(f"«{site.meta['name']}» уже устанавливается. Дождись окончания.", 409)
            os.replace(tmp_path, site.archive_path)
            self._enqueue(site, os.path.basename(filename))
        return site

    def redeploy(self, site_id: str) -> None:
        site = self.get(site_id)
        with self.lock:
            if site.deploy["active"]:
                raise SiteError("Установка уже идёт.", 409)
            if not os.path.exists(site.archive_path):
                raise SiteError("Архив не сохранился. Загрузи его заново.", 409)
            self._enqueue(site, site.meta.get("last_archive_name", "архив"))

    def update_settings(self, site_id: str, changes: dict) -> None:
        site = self.get(site_id)
        if "name" in changes:
            name = str(changes["name"]).strip()
            if not name or len(name) > 80:
                raise SiteError("Название должно быть от 1 до 80 символов.")
            site.meta["name"] = name
        if "env" in changes:
            parse_env(str(changes["env"]))
            site.meta["env"] = str(changes["env"])
        if "start_override" in changes:
            site.meta["start_override"] = str(changes["start_override"]).strip()
        if "show_in_portfolio" in changes:
            site.meta["show_in_portfolio"] = bool(changes["show_in_portfolio"])
        if "description" in changes:
            description = str(changes["description"]).strip()
            if len(description) > MAX_DESCRIPTION:
                raise SiteError(f"Описание должно быть не длиннее {MAX_DESCRIPTION} символов.")
            site.meta["description"] = description
        if "public_url" in changes:
            site.meta["public_url"] = _clean_public_url(str(changes["public_url"]))
        site.save()

    # --- портфолио ---

    def _load_portfolio(self) -> dict:
        settings = {"title": "Портфолио", "subtitle": ""}
        try:
            with open(self.portfolio_path, encoding="utf-8") as f:
                settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        return settings

    def update_portfolio(self, changes: dict) -> None:
        settings = dict(self.portfolio)
        if "title" in changes:
            title = str(changes["title"]).strip()
            if not title or len(title) > 80:
                raise SiteError("Заголовок должен быть от 1 до 80 символов.")
            settings["title"] = title
        if "subtitle" in changes:
            subtitle = str(changes["subtitle"]).strip()
            if len(subtitle) > MAX_DESCRIPTION:
                raise SiteError(f"Подзаголовок должен быть не длиннее {MAX_DESCRIPTION} символов.")
            settings["subtitle"] = subtitle
        tmp = self.portfolio_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False)
        os.replace(tmp, self.portfolio_path)
        self.portfolio = settings

    def set_cover(self, site_id: str, data: bytes) -> None:
        site = self.get(site_id)
        content_type = cover_type(data)
        if not content_type:
            raise SiteError("Обложка должна быть картинкой PNG, JPEG или WebP.")
        tmp = site.cover_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, site.cover_path)
        site.meta["cover_type"] = content_type
        site.meta["cover_version"] = int(time.time())
        site.save()

    def remove_cover(self, site_id: str) -> None:
        site = self.get(site_id)
        if os.path.exists(site.cover_path):
            os.unlink(site.cover_path)
        site.meta.pop("cover_type", None)
        site.meta.pop("cover_version", None)
        site.save()

    def read_cover(self, site_id: str) -> tuple[bytes, str] | None:
        site = self.get(site_id)
        content_type = site.meta.get("cover_type")
        if not content_type or not os.path.exists(site.cover_path):
            return None
        with open(site.cover_path, "rb") as f:
            return f.read(), content_type

    def _is_public(self, site: Site) -> bool:
        return bool(site.release()) and site.meta.get("show_in_portfolio", True)

    def public_cover(self, site_id: str) -> tuple[bytes, str] | None:
        site = self.sites.get(site_id)
        return self.read_cover(site_id) if site and self._is_public(site) else None

    def public_site(self, site: Site, host: str) -> dict:
        """Всё, что видят посетители. Новые поля сюда попадают только намеренно."""
        release = site.release() or {}
        return {
            "id": site.id,
            "name": site.meta["name"],
            "description": site.meta.get("description", ""),
            "kind": release.get("kind"),
            "online": site.state() == "running",
            "url": site.meta.get("public_url") or f"http://{host}:{site.effective_port()}/",
            "cover": site.meta.get("cover_version") or 0,
        }

    def public_state(self, host: str) -> dict:
        sites = sorted(list(self.sites.values()), key=lambda s: s.meta.get("created_at", 0), reverse=True)
        return {
            "settings": self.portfolio,
            "sites": [self.public_site(s, host) for s in sites if self._is_public(s)],
        }

    def action(self, site_id: str, action: str) -> None:
        site = self.get(site_id)
        if site.deploy["active"]:
            raise SiteError("Сайт сейчас устанавливается. Дождись окончания.", 409)
        if action == "start":
            site.launch()
        elif action == "stop":
            site.stop()
        elif action == "restart":
            site.restart()
        elif action == "rollback":
            site.rollback()
        elif action == "redeploy":
            self.redeploy(site_id)
        else:
            raise SiteError("Неизвестное действие.", 404)

    def delete(self, site_id: str) -> None:
        site = self.get(site_id)
        if site.deploy["active"]:
            raise SiteError("Сайт сейчас устанавливается. Дождись окончания.", 409)
        site.meta["desired"] = "stopped"
        site._stop_proc()
        del self.sites[site_id]
        _rmtree(site.dir)

    def state(self) -> dict:
        sites = sorted(list(self.sites.values()), key=lambda s: s.meta.get("created_at", 0), reverse=True)
        return {
            "system": self.system,
            "sites": [s.to_json() for s in sites],
            "telegram": self.telegram.to_json(),
            "portfolio": {**self.portfolio, "public_port": self.public_port},
        }

