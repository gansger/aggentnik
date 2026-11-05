#!/usr/bin/env bash
# Установка и обновление панели на Raspberry Pi. Запуск: sudo bash install.sh
set -euo pipefail

APP_USER=malinnik
APP_DIR=/opt/malinnik
STATE_DIR=/var/lib/malinnik
DATA_DIR="$STATE_DIR/data"
PANEL_PORT=7700
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужны права администратора: sudo bash install.sh" >&2
  exit 1
fi
if [ ! -f "$SOURCE_DIR/malinnik/__main__.py" ]; then
  echo "Рядом с install.sh нет папки malinnik" >&2
  exit 1
fi

echo "==> Ставлю пакеты"
apt-get update -qq
apt-get install -y -qq python3 python3-venv nodejs npm php-cli p7zip-full curl
# Поддержка RAR лежит в разделе non-free и включена не везде, поэтому без неё тоже работаем.
apt-get install -y -qq 7zip-rar >/dev/null 2>&1 \
  || apt-get install -y -qq p7zip-rar >/dev/null 2>&1 \
  || echo "    RAR недоступен: такие сайты упаковывай в ZIP или 7Z"

echo "==> Ставлю cloudflared для публикации сайтов"
install_cloudflared() {
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
      > /etc/apt/sources.list.d/cloudflared.list \
    && apt-get update -qq \
    && apt-get install -y -qq cloudflared
}
if command -v cloudflared >/dev/null 2>&1; then
  echo "    Уже установлен"
elif ! install_cloudflared; then
  # Недоступный репозиторий мешал бы всем следующим apt-get update.
  rm -f /etc/apt/sources.list.d/cloudflared.list
  echo "    Не удалось: кнопка «Опубликовать» работать не будет, остальное будет" >&2
fi

echo "==> Ставлю AmneziaWG (первый раз занимает несколько минут)"
if command -v awg-quick >/dev/null 2>&1 && command -v amneziawg-go >/dev/null 2>&1; then
  echo "    Уже установлен"
elif ! bash "$SOURCE_DIR/install-amneziawg.sh"; then
  echo "    Не удалось: VPN работать не будет, остальное будет" >&2
fi

echo "==> Настраиваю пользователя и папки"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 750 "$STATE_DIR" "$DATA_DIR"
install -d -m 755 "$APP_DIR"
rm -rf "$APP_DIR/malinnik"
cp -r "$SOURCE_DIR/malinnik" "$APP_DIR/malinnik"
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
chown -R root:root "$APP_DIR"
chmod -R a+rX "$APP_DIR"

PASSWORD=""
if [ ! -f "$DATA_DIR/config.json" ]; then
  PASSWORD="$(cd "$APP_DIR" && sudo -u "$APP_USER" python3 -m malinnik --data "$DATA_DIR" set-password --generate)"
fi

echo "==> Настраиваю сервис"
cat > /etc/systemd/system/malinnik.service <<EOF
[Unit]
Description=Malinnik site panel
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$STATE_DIR
Environment=MALINNIK_DATA=$DATA_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 -m malinnik serve --port $PANEL_PORT
Restart=always
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=$STATE_DIR

[Install]
WantedBy=multi-user.target
EOF

# Отдельный сервис от root: только он управляет VPN. Панели root не нужен.
# KillMode=process: при обновлении панели amneziawg-go продолжает работать и VPN не рвётся.
cat > /etc/systemd/system/malinnik-vpn.service <<EOF
[Unit]
Description=Malinnik VPN helper
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 -I $APP_DIR/malinnik/vpn_helper.py
RuntimeDirectory=malinnik-vpn
RuntimeDirectoryMode=0755
StateDirectory=malinnik-vpn
StateDirectoryMode=0700
KillMode=process
Restart=always
RestartSec=3
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable malinnik-vpn malinnik >/dev/null 2>&1
systemctl restart malinnik-vpn
systemctl restart malinnik

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  echo "==> Открываю порты в ufw"
  ufw allow "$PANEL_PORT/tcp" >/dev/null
  ufw allow 8100:8199/tcp >/dev/null
fi

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "$NODE_MAJOR" -lt 20 ]; then
  echo
  echo "Внимание: Node.js версии $NODE_MAJOR. Свежим Vite и Next нужна версия 20 или новее."
fi

sleep 2
if ! systemctl is-active --quiet malinnik; then
  echo "Сервис не запустился. Журнал: journalctl -u malinnik -n 50" >&2
  exit 1
fi

ADDRESS="$(hostname -I | awk '{print $1}')"
echo
if [ -s /etc/malinnik-https-domain ]; then
  echo "Готово. Панель: https://$(cat /etc/malinnik-https-domain):$PANEL_PORT"
else
  echo "Готово. Панель: http://$ADDRESS:$PANEL_PORT"
fi
if [ -n "$PASSWORD" ]; then
  echo "Пароль: $PASSWORD"
  echo "Сохрани его: повторно он не показывается."
fi
