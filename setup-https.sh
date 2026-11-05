#!/usr/bin/env bash
# Включает HTTPS для панели: сертификат Let's Encrypt с автопродлением и nginx на порту 7700.
# Запуск: sudo bash setup-https.sh домен.ru [почта]
#
# Панель при этом переходит на внутренний порт 7699, недоступный снаружи. До получения
# сертификата она продолжает работать как раньше, а при ошибке настройка откатывается.
# Скрипт можно запускать повторно, например чтобы сменить домен.
set -euo pipefail

PUBLIC_PORT=7700
PANEL_PORT=7699
WEBROOT=/var/www/malinnik-certbot
NGINX_CONF=/etc/nginx/conf.d/malinnik.conf
DROPIN_DIR=/etc/systemd/system/malinnik.service.d
DROPIN="$DROPIN_DIR/https.conf"
DOMAIN_FILE=/etc/malinnik-https-domain
RENEW_HOOK=/etc/letsencrypt/renewal-hooks/deploy/malinnik-reload-nginx

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужны права администратора: sudo bash setup-https.sh домен.ru [почта]" >&2
  exit 1
fi
if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "Запуск: sudo bash setup-https.sh домен.ru [почта]" >&2
  exit 1
fi
if ! systemctl cat malinnik >/dev/null 2>&1; then
  echo "Панель не установлена. Сначала запусти install.sh." >&2
  exit 1
fi

# Кириллические домены вроде сайт.рф сертификат принимает только в виде xn--...
DOMAIN="$(python3 -c 'import sys; print(sys.argv[1].strip().lower().rstrip(".").encode("idna").decode())' "$1" 2>/dev/null || true)"
if ! [[ "$DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]{2,}$ ]]; then
  echo "Не похоже на домен: $1" >&2
  exit 1
fi
EMAIL="${2:-}"
if [ -n "$EMAIL" ] && ! [[ "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "Не похоже на почту: $EMAIL" >&2
  exit 1
fi

echo "==> Проверяю порт 80"
PORT80_OWNER="$(ss -ltnpH 'sport = :80' 2>/dev/null || true)"
if [ -n "$PORT80_OWNER" ] && ! grep -q nginx <<<"$PORT80_OWNER"; then
  echo "Порт 80 занят другой программой, а он нужен для получения сертификата:" >&2
  echo "$PORT80_OWNER" >&2
  exit 1
fi

echo "==> Ставлю nginx и certbot"
apt-get update -qq
apt-get install -y -qq nginx certbot curl

# Страница-заглушка nginx не нужна. Сам файл остаётся в sites-available.
if [ "$(readlink -f /etc/nginx/sites-enabled/default 2>/dev/null || true)" = "/etc/nginx/sites-available/default" ]; then
  rm -f /etc/nginx/sites-enabled/default
fi

install -d -m 755 "$WEBROOT"

# На малине с отключённым IPv6 строка listen [::] не даёт nginx запуститься.
listen_v6() {
  if [ -e /proc/net/if_inet6 ]; then
    echo "listen [::]:$1;"
  fi
}

http_block() {
  cat <<EOF
server {
    listen 80;
    $(listen_v6 80)
    server_name $DOMAIN;
    server_tokens off;

    location /.well-known/acme-challenge/ {
        root $WEBROOT;
    }
    location / {
        return 301 https://$DOMAIN:$PUBLIC_PORT\$request_uri;
    }
}
EOF
}

write_http_only() {
  http_block > "$NGINX_CONF"
}

write_full() {
  local reject_block=""
  # Без этого блока сканеры, перебирающие адреса, увидели бы панель и имя домена в сертификате.
  # ssl_reject_handshake появился в nginx 1.19.4.
  if dpkg --compare-versions "$(dpkg-query -W -f='${Version}' nginx 2>/dev/null | cut -d- -f1)" ge 1.19.4; then
    reject_block="server {
    listen $PUBLIC_PORT ssl default_server;
    $(listen_v6 "$PUBLIC_PORT ssl default_server")
    ssl_reject_handshake on;
}
"
  fi
  {
    cat <<EOF
# Создано setup-https.sh. Повторный запуск скрипта перезапишет этот файл.
limit_req_zone \$binary_remote_addr zone=malinnik_login:1m rate=6r/m;

EOF
    http_block
    echo
    printf '%s\n' "$reject_block"
    cat <<EOF
server {
    listen $PUBLIC_PORT ssl;
    $(listen_v6 "$PUBLIC_PORT ssl")
    server_name $DOMAIN;
    server_tokens off;

    ssl_certificate /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:malinnik_ssl:1m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    # HSTS намеренно не включён: он действует на все порты домена и сломал бы
    # сайты малины, которые работают по HTTP на портах 8100-8199.
    add_header X-Content-Type-Options nosniff always;

    # Открыли порт по http:// — отправляем на https://.
    error_page 497 =301 https://$DOMAIN:$PUBLIC_PORT\$request_uri;

    # Архивы идут сразу в панель: она сама проверяет размер и показывает прогресс.
    client_max_body_size 0;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
    proxy_send_timeout 600s;

    proxy_http_version 1.1;
    proxy_set_header Host \$http_host;
    proxy_set_header Connection "";
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-Proto https;

    location = /api/login {
        limit_req zone=malinnik_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:$PANEL_PORT;
    }
    location / {
        proxy_pass http://127.0.0.1:$PANEL_PORT;
    }
}
EOF
  } > "$NGINX_CONF"
}

reload_nginx() {
  nginx -t -q && systemctl enable nginx >/dev/null 2>&1 && systemctl reload-or-restart nginx
}

echo "==> Настраиваю nginx на порту 80"
# При смене домена старый конфиг заменяется временным. Если сертификат не выдадут, его надо
# вернуть: панель уже за nginx и без него была бы недоступна.
PREVIOUS_CONF=""
if [ -f "$NGINX_CONF" ]; then
  PREVIOUS_CONF="$(mktemp)"
  cp "$NGINX_CONF" "$PREVIOUS_CONF"
fi
trap '[ -z "$PREVIOUS_CONF" ] || rm -f "$PREVIOUS_CONF"' EXIT

restore_previous() {
  if [ -n "$PREVIOUS_CONF" ]; then
    cp "$PREVIOUS_CONF" "$NGINX_CONF"
    nginx -t -q && systemctl reload-or-restart nginx || true
  fi
}

# Домен прежний: рабочую настройку не трогаем, в ней уже есть всё нужное для проверки.
if [ ! -f "$DROPIN" ] || [ "$(cat "$DOMAIN_FILE" 2>/dev/null || true)" != "$DOMAIN" ]; then
  write_http_only
fi
reload_nginx

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null
  ufw allow "$PUBLIC_PORT/tcp" >/dev/null
fi

if [ -n "$EMAIL" ]; then
  ACCOUNT=(--email "$EMAIL")
else
  ACCOUNT=(--register-unsafely-without-email)
fi
CERTBOT=(certbot certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --cert-name "$DOMAIN"
  --non-interactive --agree-tos --keep-until-expiring "${ACCOUNT[@]}")

explain_failure() {
  cat >&2 <<EOF

Сертификат получить не удалось. Панель продолжает работать как раньше. Проверь:
  1. A-запись домена $DOMAIN указывает на внешний адрес малины.
     Новая запись может расходиться по DNS до нескольких часов.
  2. На роутере порт 80 проброшен на малину. Let's Encrypt заходит именно на него.
  3. Провайдер не блокирует входящие соединения на порт 80.
EOF
}

echo "==> Пробная проверка домена (не расходует лимиты Let's Encrypt)"
if ! "${CERTBOT[@]}" --dry-run; then
  restore_previous
  explain_failure
  exit 1
fi

echo "==> Получаю сертификат. Запуск скрипта означает согласие с условиями Let's Encrypt:"
echo "    https://letsencrypt.org/repository/"
if ! "${CERTBOT[@]}"; then
  restore_previous
  explain_failure
  exit 1
fi

echo "==> Настраиваю автопродление"
install -d -m 755 "$(dirname "$RENEW_HOOK")"
cat > "$RENEW_HOOK" <<'EOF'
#!/bin/sh
# Новый сертификат начинает работать только после перезагрузки настроек nginx.
exec systemctl reload nginx
EOF
chmod 755 "$RENEW_HOOK"
systemctl enable --now certbot.timer >/dev/null 2>&1 || true

echo "==> Перевожу панель за nginx"
rollback() {
  echo "Настройка не удалась, возвращаю панель на прямой порт $PUBLIC_PORT." >&2
  write_http_only
  rm -f "$DROPIN" "$DOMAIN_FILE"
  systemctl daemon-reload
  nginx -t -q && systemctl reload-or-restart nginx || true
  systemctl restart malinnik || true
}

install -d -m 755 "$DROPIN_DIR"
# Отдельный файл, а не правка службы: install.sh при обновлении панели перезаписывает службу.
cat > "$DROPIN" <<EOF
[Service]
ExecStart=
ExecStart=/usr/bin/python3 -m malinnik serve --host 127.0.0.1 --port $PANEL_PORT
EOF
systemctl daemon-reload
systemctl restart malinnik

write_full
if ! reload_nginx; then
  rollback
  exit 1
fi
echo "$DOMAIN" > "$DOMAIN_FILE"

echo "==> Проверяю результат"
ok=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -fsS -o /dev/null --max-time 5 --resolve "$DOMAIN:$PUBLIC_PORT:127.0.0.1" "https://$DOMAIN:$PUBLIC_PORT/"; then
    ok=1
    break
  fi
  sleep 1
done
if [ -z "$ok" ]; then
  rollback
  exit 1
fi

RENEW_NOTE="Автопродление проверено."
if ! certbot renew --dry-run --cert-name "$DOMAIN" >/dev/null 2>&1; then
  RENEW_NOTE="Внимание: пробное продление не прошло. Проверь: sudo certbot renew --dry-run"
fi

echo
echo "Готово. Панель: https://$DOMAIN:$PUBLIC_PORT"
echo "$RENEW_NOTE"
echo "Срок сертификата и продление: sudo certbot certificates"
echo
echo "На роутере должны быть проброшены порты 80 и $PUBLIC_PORT."
echo "Если VPN пускает через себя весь трафик, панель снаружи и продление сертификата работать не будут."
echo "Пароль передавался без шифрования, поэтому смени его:"
echo "  cd /opt/malinnik && sudo -u malinnik python3 -m malinnik --data /var/lib/malinnik/data set-password && sudo systemctl restart malinnik"
