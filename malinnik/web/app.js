"use strict";

const $ = (selector, root = document) => root.querySelector(selector);

const STATES = {
  running: ["Работает", "ok"],
  starting: ["Запускается", "busy"],
  deploying: ["Устанавливается", "busy"],
  restarting: ["Перезапускается", "warn"],
  unresponsive: ["Не отвечает", "warn"],
  crashed: ["Упал", "bad"],
  failed: ["Не установлен", "bad"],
  stopped: ["Остановлен", "idle"],
};
const KINDS = { static: "Статика", node: "Node.js", python: "Python", php: "PHP", custom: "Своя команда" };
const STAGES = [
  ["extract", "Распаковка"],
  ["detect", "Анализ"],
  ["install", "Зависимости"],
  ["build", "Сборка"],
  ["start", "Запуск"],
];
const POLL_MS = 2000;

const rows = new Map();
let pollTimer = null;

/* --- форматирование --- */

function formatBytes(bytes) {
  if (bytes == null) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = value < 10 && unit > 1 ? 1 : 0;
  return `${value.toFixed(digits).replace(".", ",")} ${units[unit]}`;
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч ${minutes % 60} мин`;
  return `${Math.floor(hours / 24)} дн ${hours % 24} ч`;
}

function formatAgo(timestamp) {
  if (!timestamp) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
  return seconds < 60 ? "только что" : `${formatDuration(seconds)} назад`;
}

/* --- API --- */

class AuthError extends Error {}

async function api(path, { method = "GET", body } = {}) {
  const headers = { "X-Malinnik": "1" };
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== "/api/login") {
    showLogin();
    throw new AuthError(data.error);
  }
  if (!response.ok) throw new Error(data.error || `Ошибка ${response.status}`);
  return data;
}

function toast(message, bad = false) {
  const el = document.createElement("div");
  el.className = bad ? "toast bad" : "toast";
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), bad ? 8000 : 4000);
}

function reportError(error) {
  if (!(error instanceof AuthError)) toast(error.message, true);
}

function confirmAction(title, text, yesLabel) {
  const dialog = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  $("#confirm-yes").textContent = yesLabel;
  dialog.returnValue = "no";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "yes"), { once: true });
  });
}

/* --- вход --- */

function showLogin() {
  clearTimeout(pollTimer);
  pollTimer = null;
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#password").focus();
}

function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#login-error");
  error.textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { password: $("#password").value } });
    $("#password").value = "";
    start();
  } catch (e) {
    error.textContent = e.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

/* --- состояние малины --- */

function renderVitals(system) {
  const items = [];
  if (system.cpu != null) items.push(["CPU", `${Math.round(system.cpu)}%`]);
  if (system.temp != null) items.push(["Температура", `${Math.round(system.temp)} °C`, system.temp >= 75]);
  if (system.mem_total != null) {
    items.push(["Память", `${formatBytes(system.mem_used)} из ${formatBytes(system.mem_total)}`]);
  }
  items.push(["Диск", `свободно ${formatBytes(system.disk_free)}`, system.disk_free < 1024 ** 3]);
  if (system.uptime != null) items.push(["Включена", formatDuration(system.uptime)]);

  const list = $("#vitals");
  list.replaceChildren(...items.map(([label, value, hot]) => {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = label;
    dd.textContent = value;
    if (hot) dd.className = "hot";
    wrap.append(dt, dd);
    return wrap;
  }));

  const warning = $("#power-warning");
  const problems = [];
  if (system.undervoltage) problems.push("малине не хватает питания");
  if (system.throttled) problems.push("процессор замедлен из-за перегрева или питания");
  warning.hidden = problems.length === 0;
  warning.textContent = problems.length ? `Внимание: ${problems.join(", ")}.` : "";
}

/* --- строка сайта --- */

function createRow(site) {
  const el = $("#site-template").content.firstElementChild.cloneNode(true);
  const row = { el, site, tab: "run", open: false, settingsFilled: false, logTimer: null };
  el.dataset.id = site.id;

  $(".toggle", el).addEventListener("click", () => {
    const action = row.site.desired === "running" && row.site.state !== "crashed" ? "stop" : "start";
    runAction(row, action);
  });
  $(".restart", el).addEventListener("click", () => runAction(row, "restart"));
  $(".more", el).addEventListener("click", () => setOpen(row, !row.open));
  el.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setTab(row, tab.dataset.tab));
  });

  $(".settings", el).addEventListener("submit", (event) => {
    event.preventDefault();
    saveSettings(row);
  });
  $(".redeploy", el).addEventListener("click", async () => {
    await runAction(row, "redeploy");
    setTab(row, "deploy");
  });
  $(".rollback", el).addEventListener("click", async () => {
    const yes = await confirmAction(
      "Вернуть прошлую версию?",
      `«${row.site.name}» будет перезапущен с предыдущей версией файлов. Текущая останется доступна для обратного переключения.`,
      "Вернуть",
    );
    if (yes) runAction(row, "rollback");
  });
  $(".delete", el).addEventListener("click", async () => {
    const yes = await confirmAction(
      "Удалить сайт?",
      `«${row.site.name}» будет остановлен, его файлы, данные и журналы удалятся с малины без возможности восстановления.`,
      "Удалить навсегда",
    );
    if (!yes) return;
    try {
      await api(`/api/sites/${row.site.id}`, { method: "DELETE" });
      toast(`«${row.site.name}» удалён`);
      refresh();
    } catch (e) {
      reportError(e);
    }
  });

  bindSiteDrop(row);
  rows.set(site.id, row);
  return row;
}

async function runAction(row, action) {
  try {
    await api(`/api/sites/${row.site.id}/${action}`, { method: "POST" });
  } catch (e) {
    reportError(e);
  }
  refresh();
}

function setOpen(row, open) {
  row.open = open;
  $(".details", row.el).hidden = !open;
  const more = $(".more", row.el);
  more.setAttribute("aria-expanded", String(open));
  more.textContent = open ? "Свернуть" : "Подробнее";
  if (open) {
    setTab(row, row.tab);
  } else {
    clearTimeout(row.logTimer);
  }
}

function setTab(row, tab) {
  row.tab = tab;
  row.el.querySelectorAll(".tab").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.tab === tab));
  });
  const isSettings = tab === "settings";
  $(".settings", row.el).hidden = !isSettings;
  $(".log", row.el).hidden = isSettings;
  clearTimeout(row.logTimer);
  if (isSettings) {
    fillSettings(row);
  } else {
    $(".log", row.el).textContent = "";
    pollLog(row, true);
  }
}

async function pollLog(row, jump = false) {
  clearTimeout(row.logTimer);
  if (!row.open || row.tab === "settings" || !rows.has(row.site.id)) return;
  if (!document.hidden) {
    const log = $(".log", row.el);
    const tab = row.tab;
    try {
      const { text } = await api(`/api/sites/${row.site.id}/logs?kind=${tab}`);
      if (row.tab !== tab || !row.open) return; // пока шёл запрос, открыли другую вкладку
      const pinned = jump || log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      if (log.textContent !== text) {
        log.textContent = text || "Журнал пока пуст.";
        if (pinned) log.scrollTop = log.scrollHeight;
      }
    } catch (e) {
      if (e instanceof AuthError) return;
    }
  }
  row.logTimer = setTimeout(() => pollLog(row), POLL_MS);
}

function fillSettings(row) {
  const form = $(".settings", row.el);
  form.elements.name.value = row.site.name;
  form.elements.start_override.value = row.site.start_override;
  form.elements.env.value = row.site.env;
  $(".form-error", form).textContent = "";
  renderSettingsHints(row);
}

function renderSettingsHints(row) {
  const { site, el } = row;
  $(".hint-cmd", el).textContent = site.start_cmd
    ? `Пусто — панель сама запускает: ${site.start_cmd}`
    : "Пусто — панель определит команду сама. Порт доступен как $PORT.";
  $(".rollback", el).hidden = site.previous == null;
  $(".redeploy", el).disabled = !site.can_redeploy;
}

async function saveSettings(row) {
  const form = $(".settings", row.el);
  const error = $(".form-error", form);
  error.textContent = "";
  try {
    await api(`/api/sites/${row.site.id}`, {
      method: "PATCH",
      body: {
        name: form.elements.name.value,
        start_override: form.elements.start_override.value,
        env: form.elements.env.value,
      },
    });
    if (row.site.release != null) {
      await api(`/api/sites/${row.site.id}/restart`, { method: "POST" });
      toast("Настройки сохранены, сайт перезапущен");
    } else {
      toast("Настройки сохранены. Нажми «Пересобрать», чтобы установить сайт");
    }
    refresh();
  } catch (e) {
    if (!(e instanceof AuthError)) error.textContent = e.message;
  }
}

function renderHttp(site) {
  if (site.state === "running") {
    if (site.health.code == null) return ["порт открыт", false];
    const ms = site.health.ms != null ? ` · ${site.health.ms} мс` : "";
    return [`${site.health.code}${ms}`, site.health.code >= 500];
  }
  if (site.state === "unresponsive") return ["нет ответа", true];
  return ["—", false];
}

function renderSteps(row) {
  const { site, el } = row;
  const list = $(".steps", el);
  list.hidden = !site.deploy.active;
  if (!site.deploy.active) return;
  if (site.deploy.queued) {
    const item = document.createElement("li");
    item.textContent = "В очереди: сайты устанавливаются по одному";
    list.replaceChildren(item);
    return;
  }
  const current = STAGES.findIndex(([key]) => key === site.deploy.stage);
  list.replaceChildren(...STAGES.map(([, label], index) => {
    const item = document.createElement("li");
    item.textContent = label;
    if (index < current) item.className = "done";
    if (index === current) {
      item.className = "now";
      item.setAttribute("aria-current", "step");
    }
    return item;
  }));
}

function subtitle(site) {
  const parts = [];
  if (site.kind) parts.push(KINDS[site.kind] || site.kind);
  if (site.release != null) parts.push(`версия ${site.release}`);
  if (site.deployed_at) parts.push(`обновлён ${formatAgo(site.deployed_at)}`);
  if (site.size != null) parts.push(formatBytes(site.size));
  if (site.restarts > 0) parts.push(`перезапусков: ${site.restarts}`);
  return parts.join(" · ");
}

function statusLabel(site) {
  if (site.state === "deploying") return site.deploy.queued ? "В очереди" : STATES.deploying[0];
  if (site.state === "stopped" || !site.exit_code) return STATES[site.state][0];
  return `${STATES[site.state][0]} · код ${site.exit_code}`;
}

function renderRow(row, site) {
  const { el } = row;
  const wasDeploying = row.site.deploy.active;
  row.site = site;
  const [, tone] = STATES[site.state] || ["", "idle"];
  el.dataset.tone = tone;

  $(".status-text", el).textContent = statusLabel(site);
  $(".site-name", el).textContent = site.name;
  $(".site-sub", el).textContent = subtitle(site);

  const [http, httpBad] = renderHttp(site);
  const httpCell = $(".stat-http", el);
  httpCell.textContent = http;
  httpCell.classList.toggle("bad", httpBad);
  $(".stat-cpu", el).textContent = site.cpu != null ? `${site.cpu.toString().replace(".", ",")}%` : "—";
  $(".stat-ram", el).textContent = site.rss != null ? formatBytes(site.rss) : "—";
  $(".stat-uptime", el).textContent = formatDuration(site.uptime);

  const open = $(".open", el);
  const live = site.uptime != null;
  open.hidden = !live;
  if (live) {
    // Панель может работать по HTTPS через nginx, а порты сайтов отдают только HTTP.
    const url = `http://${location.hostname}:${site.effective_port}/`;
    if (open.href !== url) open.href = url;
    const label = `Открыть :${site.effective_port}`;
    if (open.textContent !== label) open.textContent = label;
  }

  const toggle = $(".toggle", el);
  const stopping = site.desired === "running" && site.state !== "crashed";
  toggle.textContent = stopping ? "Остановить" : "Запустить";
  toggle.hidden = site.release == null;
  toggle.disabled = site.deploy.active;
  const restart = $(".restart", el);
  restart.hidden = site.release == null || !stopping;
  restart.disabled = site.deploy.active;

  renderSteps(row);

  const error = $(".notice-error", el);
  error.hidden = !site.error;
  error.textContent = site.error || "";
  const warning = $(".notice-warning", el);
  const portNote = site.state === "running" && site.effective_port !== site.port
    ? `Сайт слушает порт ${site.effective_port} вместо выданного ${site.port}: в его коде порт задан жёстко. Чтобы сайты не конфликтовали, читай порт из переменной PORT.`
    : "";
  const warningText = site.warning || portNote;
  warning.hidden = !warningText;
  warning.textContent = warningText;

  $(".drop-hint span", el).textContent = `Обновить «${site.name}»`;
  if (row.open && row.tab === "settings") renderSettingsHints(row);

  // Установка закончилась с ошибкой: показываем, что пошло не так.
  if (wasDeploying && !site.deploy.active && site.error) {
    setOpen(row, true);
    setTab(row, "deploy");
  }
}

function renderSites(sites) {
  const list = $("#sites");
  const seen = new Set();
  sites.forEach((site, index) => {
    seen.add(site.id);
    const row = rows.get(site.id) || createRow(site);
    renderRow(row, site);
    if (list.children[index] !== row.el) list.insertBefore(row.el, list.children[index] || null);
  });
  for (const [id, row] of rows) {
    if (!seen.has(id)) {
      clearTimeout(row.logTimer);
      row.el.remove();
      rows.delete(id);
    }
  }
  $("#empty").hidden = sites.length > 0;
  document.body.classList.toggle("no-sites", sites.length === 0);
}

async function refresh() {
  const data = await api("/api/state");
  showApp();
  renderVitals(data.system);
  renderTelegram(data.telegram);
  renderSites(data.sites);
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    if (!document.hidden) await refresh();
  } catch (e) {
    if (e instanceof AuthError) return;
    // Малина могла перезагружаться: просто пробуем снова.
  }
  pollTimer = setTimeout(poll, POLL_MS);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && pollTimer) poll();
});

async function start() {
  try {
    await refresh();
    pollTimer = setTimeout(poll, POLL_MS);
  } catch (e) {
    if (!(e instanceof AuthError)) {
      showLogin();
      $("#login-error").textContent = "Панель не отвечает.";
    }
  }
}

/* --- Telegram --- */

function setStatus(el, text, tone = "") {
  el.textContent = text;
  el.className = tone ? `service-status ${tone}` : "service-status";
}

function renderTelegram(telegram) {
  const status = $("#tg-status");
  const ready = telegram.token_set && telegram.chat_id;
  if (!telegram.token_set) {
    setStatus(status, "Не настроены");
  } else if (!telegram.chat_id) {
    setStatus(status, "Токен сохранён, осталось найти чат", "warn");
  } else if (telegram.enabled) {
    setStatus(status, "Включены", "ok");
  } else {
    setStatus(status, "Выключены");
  }
  $("#tg-switch-wrap").hidden = !ready;
  $("#tg-switch").checked = telegram.enabled;
  $("#tg-test").hidden = !ready;
  $("#tg-help").hidden = !(ready && telegram.enabled);
  $("#tg-token").placeholder = telegram.token_set ? "Токен сохранён, вставь новый, чтобы заменить" : "123456:ABC-DEF…";
  const notice = $("#tg-notice");
  notice.hidden = !telegram.error;
  notice.textContent = telegram.error || "";
  notice.classList.toggle("bad", Boolean(telegram.error));
}

$("#tg-setup").addEventListener("click", (event) => {
  const form = $("#tg-form");
  form.hidden = !form.hidden;
  event.target.setAttribute("aria-expanded", String(!form.hidden));
  if (!form.hidden) $("#tg-token").focus();
});

$("#tg-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("#tg-form-error");
  const field = $("#tg-token");
  error.textContent = "";
  try {
    await api("/api/telegram", { method: "POST", body: { token: field.value } });
    field.value = "";
    toast("Токен сохранён. Теперь напиши боту и нажми «Найти мой чат»");
    refresh().catch(reportError);
  } catch (e) {
    if (!(e instanceof AuthError)) error.textContent = e.message;
  }
});

$("#tg-find").addEventListener("click", async () => {
  const error = $("#tg-form-error");
  error.textContent = "";
  try {
    const { chat } = await api("/api/telegram/find-chat", { method: "POST" });
    await api("/api/telegram", { method: "POST", body: { enabled: true } });
    await api("/api/telegram/test", { method: "POST" });
    $("#tg-form").hidden = true;
    $("#tg-setup").setAttribute("aria-expanded", "false");
    toast(`Чат «${chat}» найден, проверочное сообщение отправлено`);
    refresh().catch(reportError);
  } catch (e) {
    if (!(e instanceof AuthError)) error.textContent = e.message;
  }
});

$("#tg-test").addEventListener("click", async () => {
  try {
    await api("/api/telegram/test", { method: "POST" });
    toast("Проверочное сообщение отправлено");
  } catch (e) {
    reportError(e);
  }
});

$("#tg-switch").addEventListener("change", async (event) => {
  try {
    await api("/api/telegram", { method: "POST", body: { enabled: event.target.checked } });
  } catch (e) {
    reportError(e);
  }
  refresh().catch(reportError);
});

/* --- загрузка --- */

function uploadFile(file, site) {
  const item = document.createElement("li");
  item.className = "upload";
  const name = document.createElement("span");
  name.className = "upload-name";
  name.textContent = site ? `${file.name} → ${site.name}` : file.name;
  const info = document.createElement("span");
  info.className = "upload-info";
  info.textContent = "0%";
  const cancel = document.createElement("button");
  cancel.className = "btn ghost";
  cancel.textContent = "Отменить";
  const bar = document.createElement("div");
  bar.className = "bar";
  const fill = document.createElement("i");
  bar.append(fill);
  item.append(name, info, cancel, bar);
  $("#uploads").append(item);

  const params = new URLSearchParams({ filename: file.name });
  if (site) params.set("site", site.id);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?${params}`);
  xhr.setRequestHeader("X-Malinnik", "1");
  xhr.setRequestHeader("Content-Type", "application/octet-stream");

  const startedAt = performance.now();
  xhr.upload.addEventListener("progress", (event) => {
    if (!event.lengthComputable) return;
    const percent = Math.round((event.loaded / event.total) * 100);
    const seconds = (performance.now() - startedAt) / 1000;
    const speed = seconds > 0.5 ? ` · ${formatBytes(event.loaded / seconds)}/с` : "";
    fill.style.width = `${percent}%`;
    info.textContent = `${percent}% из ${formatBytes(event.total)}${speed}`;
  });
  xhr.upload.addEventListener("load", () => {
    info.textContent = "Передано, начинаю установку…";
    cancel.hidden = true;
  });
  cancel.addEventListener("click", () => xhr.abort());
  xhr.addEventListener("abort", () => item.remove());
  xhr.addEventListener("error", () => {
    item.remove();
    toast(`Не удалось загрузить ${file.name}: связь с малиной оборвалась`, true);
  });
  xhr.addEventListener("load", async () => {
    item.remove();
    let data = {};
    try {
      data = JSON.parse(xhr.responseText);
    } catch { /* ответ не JSON: покажем общую ошибку */ }
    if (xhr.status === 401) {
      showLogin();
      return;
    }
    if (xhr.status !== 200) {
      toast(data.error || `Не удалось загрузить ${file.name}`, true);
      return;
    }
    try {
      await refresh();
    } catch (e) {
      reportError(e);
    }
    // При загрузке нескольких архивов разворачиваем журнал только у первого.
    const row = rows.get(data.id);
    if (row && ![...rows.values()].some((other) => other.open)) {
      setOpen(row, true);
      setTab(row, "deploy");
    }
  });
  xhr.send(file);
}

function handleDrop(dataTransfer, site) {
  const entries = [...dataTransfer.items || []];
  if (entries.some((entry) => entry.webkitGetAsEntry && entry.webkitGetAsEntry()?.isDirectory)) {
    toast("Папку загрузить нельзя. Упакуй её: правый клик → «Сжать в ZIP-файл»", true);
    return;
  }
  const files = [...dataTransfer.files];
  if (site && files.length > 1) {
    toast("Для обновления сайта нужен один архив", true);
    return;
  }
  files.forEach((file) => uploadFile(file, site));
}

let dragDepth = 0;

function isFileDrag(event) {
  return [...(event.dataTransfer?.types || [])].includes("Files");
}

function endDrag() {
  dragDepth = 0;
  document.body.classList.remove("dragging");
  $("#drag-overlay").hidden = true;
  document.querySelectorAll(".site.drop-target").forEach((el) => el.classList.remove("drop-target"));
  $("#drag-overlay").classList.remove("on-site");
}

function bindSiteDrop(row) {
  const { el } = row;
  el.addEventListener("dragover", (event) => {
    if (!isFileDrag(event) || row.site.deploy.active) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
    el.classList.add("drop-target");
    $("#drag-overlay").classList.add("on-site");
  });
  el.addEventListener("dragleave", (event) => {
    if (!el.contains(event.relatedTarget)) {
      el.classList.remove("drop-target");
      $("#drag-overlay").classList.remove("on-site");
    }
  });
  el.addEventListener("drop", (event) => {
    if (!isFileDrag(event) || row.site.deploy.active) return;
    event.preventDefault();
    event.stopPropagation();
    const dataTransfer = event.dataTransfer;
    endDrag();
    handleDrop(dataTransfer, row.site);
  });
}

document.addEventListener("dragenter", (event) => {
  if (!isFileDrag(event) || $("#app").hidden) return;
  dragDepth += 1;
  document.body.classList.add("dragging");
  $("#drag-overlay").hidden = false;
});
document.addEventListener("dragleave", (event) => {
  if (!isFileDrag(event)) return;
  dragDepth -= 1;
  if (dragDepth <= 0) endDrag();
});
document.addEventListener("dragover", (event) => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
document.addEventListener("drop", (event) => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  const dataTransfer = event.dataTransfer;
  const wasHidden = $("#app").hidden;
  endDrag();
  if (!wasHidden) handleDrop(dataTransfer, null);
});

const dropZone = $("#drop");
const fileInput = $("#file");
dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});
fileInput.addEventListener("click", (event) => event.stopPropagation());
fileInput.addEventListener("change", () => {
  [...fileInput.files].forEach((file) => uploadFile(file, null));
  fileInput.value = "";
});

start();
