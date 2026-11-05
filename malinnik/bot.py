"""Команды Telegram-бота: состояние, журналы и управление сайтами и VPN.

Бот управляет сервером, поэтому слушается только привязанного личного чата. В группе команды
не работают: там ими мог бы пользоваться любой участник. Чужие сообщения бот молча игнорирует,
чтобы не подтверждать посторонним, что он работает. Удаления сайтов и загрузки кода здесь нет.
"""
from __future__ import annotations

import logging
import queue
import secrets
import threading
import time

from .sites import SiteError
from .telegram import TelegramError

log = logging.getLogger("malinnik")

MAX_COMMAND_AGE = 120  # команда, пролежавшая в очереди, пока панель была выключена, уже не актуальна
CONFIRM_SECONDS = 300
MAX_TEXT = 3900
LOG_LINES = 30
LOG_CHARS = 3500

STATES = {
    "running": ("🟢", "работает"),
    "starting": ("🟡", "запускается"),
    "deploying": ("🟡", "устанавливается"),
    "restarting": ("🟠", "перезапускается"),
    "unresponsive": ("🟠", "не отвечает"),
    "crashed": ("🔴", "упал"),
    "failed": ("🔴", "не установлен"),
    "stopped": ("⚪", "остановлен"),
}
KINDS = {"static": "Статика", "node": "Node.js", "python": "Python", "php": "PHP", "custom": "Своя команда"}
HELP = """Что я умею:

/status — малина, VPN и сайты
/sites — список сайтов с кнопками
/site имя — подробно о сайте
/logs имя — журнал сайта
/restart имя — перезапустить
/run имя — запустить
/stop имя — остановить
/publish имя — открыть в интернете
/unpublish имя — снять с публикации
/vpn — состояние VPN, /vpn on или /vpn off

Имя можно не указывать: я предложу выбрать сайт. Хватит и начала имени."""
COMMANDS = [
    ("status", "Малина, VPN и сайты"),
    ("sites", "Список сайтов"),
    ("site", "Подробно о сайте"),
    ("logs", "Журнал сайта"),
    ("restart", "Перезапустить сайт"),
    ("run", "Запустить сайт"),
    ("stop", "Остановить сайт"),
    ("publish", "Открыть сайт в интернете"),
    ("unpublish", "Снять сайт с публикации"),
    ("vpn", "Состояние VPN"),
    ("help", "Справка"),
]
KEYBOARD = {
    "keyboard": [[{"text": "Статус"}, {"text": "Сайты"}, {"text": "VPN"}]],
    "resize_keyboard": True,
}
KEYBOARD_WORDS = {"статус": "status", "сайты": "sites", "vpn": "vpn"}
SITE_ACTIONS = {"restart": "restart", "run": "start", "stop": "stop", "publish": "publish", "unpublish": "unpublish"}
CONFIRMED = {"stop", "publish", "unpublish", "vpn_up", "vpn_down"}
PICK_TITLES = {
    "restart": "Какой сайт перезапустить?",
    "start": "Какой сайт запустить?",
    "stop": "Какой сайт остановить?",
    "publish": "Какой сайт открыть в интернете?",
    "unpublish": "Какой сайт снять с публикации?",
    "logs": "Журнал какого сайта показать?",
    "card": "О каком сайте рассказать?",
}


def format_bytes(value) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}".replace(".", ",") if unit in ("ГБ",) else f"{round(size)} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ".replace(".", ",")


def format_duration(seconds) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч {minutes % 60} мин"
    return f"{hours // 24} дн {hours % 24} ч"


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


class Bot:
    def __init__(self, manager):
        self.manager = manager
        self.telegram = manager.telegram
        self.jobs: queue.Queue = queue.Queue()
        self.pending: dict[str, tuple[str, str | None, float]] = {}

    def attach(self) -> None:
        self.telegram.commands = [{"command": name, "description": text} for name, text in COMMANDS]
        self.telegram.handler = self.jobs.put
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        # Действия идут по одному: поднятие VPN занимает до минуты, а приём сообщений не должен ждать.
        while True:
            update = self.jobs.get()
            try:
                self.handle(update)
            except TelegramError as exc:
                log.warning("Telegram: %s", exc)
            except Exception:  # noqa: BLE001 — одна команда не должна останавливать бота
                log.exception("Ошибка команды Telegram")

    # --- доступ ---

    def _allowed(self, chat: dict | None) -> bool:
        settings = self.telegram.settings
        allowed = bool(
            chat and settings["enabled"] and settings["chat_id"]
            and str(chat.get("id")) == settings["chat_id"] and chat.get("type") == "private"
        )
        if chat and not allowed and settings["enabled"]:
            log.warning("Telegram: сообщение от чужого чата %s проигнорировано", chat.get("id"))
        return allowed

    def handle(self, update: dict) -> None:
        query = update.get("callback_query")
        if query:
            message = query.get("message") or {}
            if self._allowed(message.get("chat")):
                self._on_button(query, message)
            return
        message = update.get("message")
        if message and self._allowed(message.get("chat")):
            self._on_message(message)

    # --- отправка ---

    def _send(self, chat_id, text: str, buttons=None, keyboard=None, pre=False) -> None:
        text = text[:MAX_TEXT]
        payload = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        elif keyboard:
            payload["reply_markup"] = keyboard
        if pre:
            # Telegram считает длину в единицах UTF-16.
            length = len(text.encode("utf-16-le")) // 2
            payload["entities"] = [{"type": "pre", "offset": 0, "length": length}]
        self.telegram._call("sendMessage", payload)

    def _show(self, chat_id, message_id, text: str, buttons=None) -> None:
        """Обновляет сообщение с кнопками, а не шлёт новое, чтобы не засорять чат."""
        if message_id is None:
            self._send(chat_id, text, buttons)
            return
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT],
                   "reply_markup": {"inline_keyboard": buttons or []}}
        try:
            self.telegram._call("editMessageText", payload)
        except TelegramError as exc:
            if "not modified" not in str(exc):
                raise

    # --- сообщения ---

    def _on_message(self, message: dict) -> None:
        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        if not text:
            return
        if time.time() - message.get("date", 0) > MAX_COMMAND_AGE:
            self._send(chat_id, f"Команда «{text[:40]}» пришла давно, пока панель была недоступна. Не выполняю, повтори её.")
            return
        word, _, arg = text.partition(" ")
        arg = arg.strip()
        if word.startswith("/"):
            command = word[1:].split("@", 1)[0].lower()
        else:
            command = KEYBOARD_WORDS.get(text.lower())
        try:
            self._command(chat_id, command, arg)
        except SiteError as exc:
            self._send(chat_id, str(exc))

    def _command(self, chat_id, command: str | None, arg: str) -> None:
        if command in ("start", "help"):
            self._send(chat_id, HELP, keyboard=KEYBOARD)
        elif command == "status":
            self._send(chat_id, self._status_text(), [[button("Обновить", "status"), button("Сайты", "sites")]])
        elif command == "sites":
            self._show_sites(chat_id, None)
        elif command == "vpn":
            self._vpn_command(chat_id, arg.lower())
        elif command == "site":
            self._with_site(chat_id, arg, "card", lambda site: self._show_card(chat_id, None, site["id"]))
        elif command == "logs":
            self._with_site(chat_id, arg, "logs", lambda site: self._send_logs(chat_id, site["id"], "run"))
        elif command in SITE_ACTIONS:
            action = SITE_ACTIONS[command]
            self._with_site(chat_id, arg, action, lambda site: self._request(chat_id, None, action, site["id"]))
        else:
            self._send(chat_id, "Не знаю такой команды. Список: /help", keyboard=KEYBOARD)

    def _vpn_command(self, chat_id, arg: str) -> None:
        if arg in ("on", "вкл", "включить"):
            self._request(chat_id, None, "vpn_up", None)
        elif arg in ("off", "выкл", "выключить"):
            self._request(chat_id, None, "vpn_down", None)
        else:
            self._show_vpn(chat_id, None)

    def _with_site(self, chat_id, arg: str, action: str, run) -> None:
        sites = self.manager.state()["sites"]
        if not sites:
            self._send(chat_id, "Сайтов пока нет. Загрузи архив в панели.")
            return
        if not arg:
            prefix = "ask" if action in CONFIRMED else "do"
            buttons = [[button(s["name"], f"{prefix}:{action}:{s['id']}")] for s in sites]
            self._send(chat_id, PICK_TITLES[action], buttons)
            return
        matches = self._find_sites(sites, arg)
        if len(matches) == 1:
            run(matches[0])
        elif matches:
            names = ", ".join(s["name"] for s in matches)
            self._send(chat_id, f"Подходит несколько сайтов: {names}. Уточни имя.")
        else:
            self._send(chat_id, f"Сайта «{arg[:40]}» нет. Список: /sites")

    @staticmethod
    def _find_sites(sites: list[dict], arg: str) -> list[dict]:
        wanted = arg.casefold()
        exact = [s for s in sites if wanted in (s["id"].casefold(), s["name"].casefold())]
        if exact:
            return exact
        return [s for s in sites if s["name"].casefold().startswith(wanted) or s["id"].casefold().startswith(wanted)]

    # --- кнопки ---

    def _on_button(self, query: dict, message: dict) -> None:
        chat_id = message["chat"]["id"]
        message_id = message.get("message_id")
        self.telegram._call("answerCallbackQuery", {"callback_query_id": query["id"]})
        kind, _, rest = query.get("data", "").partition(":")
        try:
            if kind == "status":
                self._show(chat_id, message_id, self._status_text(),
                           [[button("Обновить", "status"), button("Сайты", "sites")]])
            elif kind == "sites":
                self._show_sites(chat_id, message_id)
            elif kind == "vpn":
                self._show_vpn(chat_id, message_id)
            elif kind == "card":
                self._show_card(chat_id, message_id, rest)
            elif kind == "logs":
                source, _, site_id = rest.partition(":")
                self._send_logs(chat_id, site_id, source)
            elif kind == "do":
                action, _, site_id = rest.partition(":")
                if action == "card":
                    self._show_card(chat_id, message_id, site_id)
                elif action == "logs":
                    self._send_logs(chat_id, site_id, "run")
                elif action not in CONFIRMED:
                    self._perform(chat_id, message_id, action, site_id)
            elif kind == "ask":
                action, _, site_id = rest.partition(":")
                self._request(chat_id, message_id, action, site_id or None)
            elif kind == "yes":
                self._confirmed(chat_id, message_id, rest)
            elif kind == "no":
                self._show(chat_id, message_id, "Отменено.")
        except SiteError as exc:
            self._send(chat_id, str(exc))

    def _request(self, chat_id, message_id, action: str, site_id: str | None) -> None:
        """Опасное действие сначала спрашивает подтверждение, безопасное выполняет сразу."""
        if action not in CONFIRMED:
            self._perform(chat_id, message_id, action, site_id)
            return
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        token = secrets.token_urlsafe(8)
        self.pending[token] = (action, site_id, now + CONFIRM_SECONDS)
        buttons = [[button("Да", f"yes:{token}"), button("Отмена", "no")]]
        self._show(chat_id, message_id, self._question(action, site_id), buttons)

    def _question(self, action: str, site_id: str | None) -> str:
        name = self.manager.get(site_id).meta["name"] if site_id else ""
        return {
            "stop": f"Остановить «{name}»?",
            "publish": f"Открыть «{name}» в интернете? Сайт увидят все, у кого есть ссылка.",
            "unpublish": f"Снять «{name}» с публикации? Ссылка перестанет работать.",
            "vpn_up": ("Включить VPN? Через него пойдёт интернет-трафик малины, включая этого бота. "
                       "Если сервер не ответит, бот замолчит, и выключить VPN можно будет только из панели."),
            "vpn_down": "Выключить VPN? Трафик малины пойдёт напрямую.",
        }[action]

    def _confirmed(self, chat_id, message_id, token: str) -> None:
        entry = self.pending.pop(token, None)
        if not entry or entry[2] < time.time():
            self._show(chat_id, message_id, "Эта кнопка устарела. Отправь команду ещё раз.")
            return
        self._perform(chat_id, message_id, entry[0], entry[1])

    def _perform(self, chat_id, message_id, action: str, site_id: str | None) -> None:
        if action in ("vpn_up", "vpn_down"):
            self._show(chat_id, message_id, "Включаю VPN…" if action == "vpn_up" else "Выключаю VPN…")
            self.manager.vpn_command("up" if action == "vpn_up" else "down")
            self._show_vpn(chat_id, message_id)
            return
        self.manager.action(site_id, action)
        self._show_card(chat_id, message_id, site_id)

    # --- содержимое ---

    def _status_text(self) -> str:
        state = self.manager.state()
        system = state["system"]
        vitals = []
        if system.get("temp") is not None:
            vitals.append(f"{round(system['temp'])} °C")
        if system.get("cpu") is not None:
            vitals.append(f"CPU {round(system['cpu'])}%")
        if system.get("mem_total"):
            vitals.append(f"память {format_bytes(system['mem_used'])} из {format_bytes(system['mem_total'])}")
        vitals.append(f"диск свободно {format_bytes(system.get('disk_free'))}")
        if system.get("uptime") is not None:
            vitals.append(f"включена {format_duration(system['uptime'])}")
        lines = ["Малина: " + " · ".join(vitals)]
        if system.get("undervoltage"):
            lines.append("⚠️ Не хватает питания")
        if system.get("throttled"):
            lines.append("⚠️ Процессор замедлен")
        lines.append("VPN: " + self._vpn_line(state["vpn"]))
        lines.append("")
        lines.append(f"Сайты ({len(state['sites'])}):" if state["sites"] else "Сайтов пока нет.")
        for site in state["sites"]:
            icon, label = STATES.get(site["state"], ("⚪", site["state"]))
            public = " · в интернете" if site["tunnel"]["url"] else ""
            lines.append(f"{icon} {site['name']}: {label}, порт {site['effective_port']}{public}")
        return "\n".join(lines)

    @staticmethod
    def _vpn_line(vpn: dict) -> str:
        if not vpn.get("available"):
            return "сервис не запущен"
        if not vpn.get("installed"):
            return "AmneziaWG не установлен"
        if not vpn.get("configured"):
            return "ключ не добавлен"
        if vpn.get("up"):
            age = vpn.get("handshake_age")
            return "подключён" if age is not None and age <= 180 else "включён, но сервер не отвечает"
        return "подключается" if vpn.get("desired") else "выключен"

    def _show_sites(self, chat_id, message_id) -> None:
        sites = self.manager.state()["sites"]
        if not sites:
            self._show(chat_id, message_id, "Сайтов пока нет. Загрузи архив в панели.")
            return
        buttons = []
        for site in sites:
            icon = STATES.get(site["state"], ("⚪", ""))[0]
            buttons.append([button(f"{icon} {site['name']}", f"card:{site['id']}")])
        self._show(chat_id, message_id, "Выбери сайт:", buttons)

    def _site_json(self, site_id: str) -> dict:
        for site in self.manager.state()["sites"]:
            if site["id"] == site_id:
                return site
        raise SiteError("Такого сайта больше нет.", 404)

    def _show_card(self, chat_id, message_id, site_id: str) -> None:
        site = self._site_json(site_id)
        icon, label = STATES.get(site["state"], ("⚪", site["state"]))
        lines = [site["name"], f"{icon} {label.capitalize()}"]
        if site["kind"]:
            lines.append(f"{KINDS.get(site['kind'], site['kind'])}, версия {site['release']}")
        lines.append(f"Порт {site['effective_port']}")
        if site["uptime"] is not None:
            lines.append(f"Работает {format_duration(site['uptime'])}")
        if site["health"]["code"] is not None:
            lines.append(f"Ответ {site['health']['code']}, {site['health']['ms']} мс")
        if site["cpu"] is not None:
            lines.append(f"CPU {site['cpu']}%, память {format_bytes(site['rss'])}")
        if site["restarts"]:
            lines.append(f"Перезапусков: {site['restarts']}")
        tunnel = site["tunnel"]
        if tunnel["url"]:
            lines.append(f"В интернете: {tunnel['url']}")
        elif tunnel["state"] != "off":
            lines.append("Публикация: получаю адрес…")
        if site["error"]:
            lines.append(f"⚠️ {site['error']}")
        self._show(chat_id, message_id, "\n".join(lines), self._card_buttons(site))

    @staticmethod
    def _card_buttons(site: dict) -> list:
        site_id = site["id"]
        rows = []
        if site["release"] is not None and not site["deploy"]["active"]:
            if site["desired"] == "running" and site["state"] != "crashed":
                rows.append([button("Перезапустить", f"do:restart:{site_id}"), button("Остановить", f"ask:stop:{site_id}")])
            else:
                rows.append([button("Запустить", f"do:start:{site_id}")])
        rows.append([button("Журнал сайта", f"logs:run:{site_id}"), button("Журнал установки", f"logs:deploy:{site_id}")])
        if site["published"]:
            row = [button("Снять с публикации", f"ask:unpublish:{site_id}")]
            if site["tunnel"]["url"]:
                row.insert(0, {"text": "Открыть", "url": site["tunnel"]["url"]})
            rows.append(row)
        elif site["release"] is not None:
            rows.append([button("Открыть в интернете", f"ask:publish:{site_id}")])
        rows.append([button("Обновить", f"card:{site_id}"), button("Все сайты", "sites")])
        return rows

    def _send_logs(self, chat_id, site_id: str, source: str) -> None:
        site = self.manager.get(site_id)
        target = site.deploy_log if source == "deploy" else site.run_log
        text = target.tail(LOG_LINES).strip()
        if not text:
            self._send(chat_id, "Журнал пока пуст.")
            return
        self._send(chat_id, text[-LOG_CHARS:], pre=True)

    def _show_vpn(self, chat_id, message_id) -> None:
        vpn = self.manager.vpn_status
        lines = ["VPN: " + self._vpn_line(vpn)]
        if vpn.get("endpoint"):
            lines.append(f"Сервер {vpn['endpoint']}")
        if vpn.get("up"):
            if vpn.get("handshake_age") is not None:
                lines.append(f"Обмен ключами {format_duration(vpn['handshake_age'])} назад")
            lines.append(f"Принято {format_bytes(vpn.get('rx'))}, отправлено {format_bytes(vpn.get('tx'))}")
        if vpn.get("problem"):
            lines.append(f"⚠️ {vpn['problem']}")
        buttons = []
        if vpn.get("available") and vpn.get("configured"):
            toggle = button("Выключить", "ask:vpn_down:") if vpn.get("desired") else button("Включить", "ask:vpn_up:")
            buttons.append([toggle, button("Обновить", "vpn")])
        self._show(chat_id, message_id, "\n".join(lines), buttons or None)
