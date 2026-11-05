"""Запуск и остановка дочерних процессов, журналы."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import threading

LOG_LIMIT = 5 * 1024 * 1024
IS_POSIX = os.name == "posix"
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


def spawn(command: str, cwd: str, env: dict) -> subprocess.Popen:
    # Своя сессия нужна, чтобы остановить всё дерево процессов целиком: npm → sh → node.
    options = {"start_new_session": True} if IS_POSIX else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return subprocess.Popen(
        command, shell=True, cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **options,
    )


def kill(proc: subprocess.Popen, grace: float = 8) -> None:
    if not IS_POSIX:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.poll()
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # добиваем и то, что осталось в группе
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


class LogFile:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()

    def write(self, data: bytes) -> None:
        data = ANSI.sub(b"", data).replace(b"\r\n", b"\n")
        with self.lock:
            try:
                if os.path.getsize(self.path) > LOG_LIMIT:
                    os.replace(self.path, self.path + ".1")
            except OSError:
                pass
            with open(self.path, "ab") as f:
                f.write(data)

    def line(self, text: str) -> None:
        self.write(text.encode("utf-8") + b"\n")

    def clear(self) -> None:
        with self.lock:
            open(self.path, "wb").close()

    def tail(self, lines: int = 400) -> str:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 256 * 1024))
                data = f.read()
        except OSError:
            return ""
        return "\n".join(data.decode("utf-8", "replace").split("\n")[-lines:])
