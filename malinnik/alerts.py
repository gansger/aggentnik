"""Решает, о чём сообщить в Telegram. Шлёт только смену состояния, а не каждый замер."""
from __future__ import annotations

import time
from typing import Callable

SITE_DOWN_AFTER = 45  # короткие перезапуски не повод будить человека
VPN_DOWN_AFTER = 180
TEMP_HOT, TEMP_OK = 80, 72
DISK_LOW, DISK_OK = 1024 ** 3, 2 * 1024 ** 3
SITE_BAD_STATES = {"crashed", "restarting", "unresponsive"}


class Alerts:
    def __init__(self, notify: Callable[[str], None]):
        self.notify = notify
        self.bad_since: dict[str, float] = {}
        self.active: set[str] = set()

    def _track(self, key: str, bad: bool, on_bad: str, on_ok: str | None, after: float = 0) -> None:
        if not bad:
            self.bad_since.pop(key, None)
            if key in self.active:
                self.active.discard(key)
                if on_ok:
                    self.notify(on_ok)
            return
        since = self.bad_since.setdefault(key, time.time())
        if key not in self.active and time.time() - since >= after:
            self.active.add(key)
            self.notify(on_bad)

    def check(self, sites: list[dict], system: dict, vpn: dict) -> None:
        for site in sites:
            bad = site["desired"] == "running" and site["state"] in SITE_BAD_STATES
            self._track(
                f"site:{site['id']}", bad,
                f"«{site['name']}» не работает: {_site_problem(site)}",
                f"«{site['name']}» снова работает.",
                SITE_DOWN_AFTER,
            )
        known = {f"site:{site['id']}" for site in sites}
        for key in [k for k in self.active | set(self.bad_since) if k.startswith("site:") and k not in known]:
            self.active.discard(key)  # сайт удалён
            self.bad_since.pop(key, None)

        # Пороги разведены, чтобы значение около границы не слало сообщения по кругу.
        temp = system.get("temp")
        if temp is not None:
            if temp >= TEMP_HOT:
                self._track("temp", True, f"Малина перегревается: {round(temp)} °C.", None)
            elif temp < TEMP_OK:
                self._track("temp", False, "", f"Температура малины в норме: {round(temp)} °C.")
        free = system.get("disk_free")
        if free is not None:
            if free < DISK_LOW:
                self._track("disk", True, "На диске малины меньше 1 ГБ свободного места.", None)
            elif free > DISK_OK:
                self._track("disk", False, "", "На диске малины снова есть место.")
        if system.get("undervoltage") is not None:
            self._track("power", system["undervoltage"], "Малине не хватает питания.", "Питание малины в норме.")

        age = vpn.get("handshake_age")
        vpn_bad = bool(vpn.get("up")) and (age is None or age > VPN_DOWN_AFTER)
        self._track("vpn", vpn_bad, "VPN включён, но сервер не отвечает.", "Связь с сервером VPN восстановилась.",
                    VPN_DOWN_AFTER if age is None else 0)
        self._track("vpn-problem", bool(vpn.get("problem")), str(vpn.get("problem")), None)


def _site_problem(site: dict) -> str:
    if site["state"] == "crashed":
        return "падал несколько раз подряд, перезапуск отключён."
    if site["state"] == "unresponsive":
        return "процесс запущен, но порт не отвечает."
    code = site.get("exit_code")
    return f"процесс завершился с кодом {code}, пробую перезапустить." if code is not None else "пробую перезапустить."
