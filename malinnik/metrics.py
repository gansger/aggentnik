"""Показатели малины и сайтов из /proc. Вне Linux возвращает пустые значения."""
from __future__ import annotations

import os
import shutil
import threading
import time

PROC = "/proc"
THERMAL = "/sys/class/thermal/thermal_zone0/temp"
THROTTLED = "/sys/devices/platform/soc/soc:firmware/get_throttled"
HAS_PROC = os.path.isdir(PROC) and os.path.exists(os.path.join(PROC, "stat"))


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_stat(pid: str) -> tuple[int, int, int] | None:
    """(session, utime + stime, rss в страницах)."""
    text = _read(f"{PROC}/{pid}/stat")
    if not text:
        return None
    # Имя процесса в скобках может содержать пробелы, поэтому режем по последней скобке.
    fields = text[text.rfind(")") + 2:].split()
    try:
        return int(fields[3]), int(fields[11]) + int(fields[12]), int(fields[21])
    except (IndexError, ValueError):
        return None


def _listening_inodes() -> dict[str, int]:
    """inode сокета → порт для всех TCP-сокетов в состоянии LISTEN."""
    result: dict[str, int] = {}
    for name in ("tcp", "tcp6"):
        text = _read(f"{PROC}/net/{name}")
        for line in (text or "").splitlines()[1:]:
            cols = line.split()
            if len(cols) > 9 and cols[3] == "0A":
                result[cols[9]] = int(cols[1].rsplit(":", 1)[1], 16)
    return result


class Sampler:
    """Считает CPU, память и открытые порты по сессиям процессов (у каждого сайта своя)."""

    def __init__(self) -> None:
        self.previous: dict[int, tuple[float, int]] = {}
        self.cpu_previous: tuple[int, int] | None = None
        self.cores = os.cpu_count() or 1
        self.lock = threading.Lock()

    def sites(self, sessions: set[int]) -> dict[int, dict]:
        if not HAS_PROC or not sessions:
            return {}
        with self.lock:  # вызывается и из потока показателей, и из установки
            return self._sites(sessions)

    def _sites(self, sessions: set[int]) -> dict[int, dict]:
        now = time.monotonic()
        ticks = os.sysconf("SC_CLK_TCK")
        page = os.sysconf("SC_PAGE_SIZE")
        inodes = _listening_inodes()
        totals = {s: {"ticks": 0, "rss": 0, "listen": set()} for s in sessions}

        for pid in os.listdir(PROC):
            if not pid.isdigit():
                continue
            stat = _read_stat(pid)
            if not stat or stat[0] not in totals:
                continue
            bucket = totals[stat[0]]
            bucket["ticks"] += stat[1]
            bucket["rss"] += stat[2] * page
            try:
                for fd in os.listdir(f"{PROC}/{pid}/fd"):
                    link = os.readlink(f"{PROC}/{pid}/fd/{fd}")
                    if link.startswith("socket:["):
                        port = inodes.get(link[8:-1])
                        if port:
                            bucket["listen"].add(port)
            except OSError:
                pass

        result = {}
        for session, bucket in totals.items():
            cpu = None
            before = self.previous.get(session)
            if before and now > before[0]:
                delta = max(0, bucket["ticks"] - before[1])
                cpu = round(delta / ticks / (now - before[0]) / self.cores * 100, 1)
            self.previous[session] = (now, bucket["ticks"])
            result[session] = {"cpu": cpu, "rss": bucket["rss"], "listen": sorted(bucket["listen"])}
        for session in set(self.previous) - sessions:
            del self.previous[session]
        return result

    def system(self, path: str) -> dict:
        disk = shutil.disk_usage(path)
        info = {
            "disk_used": disk.used,
            "disk_total": disk.total,
            "disk_free": disk.free,
            "cpu": None, "mem_used": None, "mem_total": None,
            "temp": None, "uptime": None, "load": None, "undervoltage": None, "throttled": None,
        }
        if not HAS_PROC:
            return info

        first = (_read(f"{PROC}/stat") or "").split("\n", 1)[0].split()[1:]
        if first:
            values = [int(v) for v in first]
            idle = values[3] + (values[4] if len(values) > 4 else 0)
            total = sum(values[:8])
            if self.cpu_previous and total > self.cpu_previous[0]:
                busy = 1 - (idle - self.cpu_previous[1]) / (total - self.cpu_previous[0])
                info["cpu"] = round(max(0.0, busy) * 100, 1)
            self.cpu_previous = (total, idle)

        mem = {}
        for line in (_read(f"{PROC}/meminfo") or "").splitlines():
            key, _, value = line.partition(":")
            mem[key] = value.split()[0] if value.split() else "0"
        if "MemTotal" in mem and "MemAvailable" in mem:
            total = int(mem["MemTotal"]) * 1024
            info["mem_total"] = total
            info["mem_used"] = total - int(mem["MemAvailable"]) * 1024

        temp = _read(THERMAL)
        if temp and temp.strip().isdigit():
            info["temp"] = round(int(temp) / 1000, 1)
        uptime = _read(f"{PROC}/uptime")
        if uptime:
            info["uptime"] = int(float(uptime.split()[0]))
        load = _read(f"{PROC}/loadavg")
        if load:
            info["load"] = float(load.split()[0])
        throttled = _read(THROTTLED)
        if throttled:
            try:
                flags = int(throttled.strip(), 16)
                info["undervoltage"] = bool(flags & 0x1)
                info["throttled"] = bool(flags & 0x4)
            except ValueError:
                pass
        return info
