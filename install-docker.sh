#!/usr/bin/env bash
# Установка и обновление Аггентника. Запускается на самом сервере:
#
#   sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/gansger/aggentnik/main/install-docker.sh)"
#
# Повторный запуск обновляет панель или меняет домен. Сайты, пароль и настройки сохраняются.
set -euo pipefail

INSTALL_COMMAND='sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/gansger/aggentnik/main/install-docker.sh)"'
KEEP_INBOUND_COMMAND='sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/gansger/aggentnik/main/keep-inbound.sh)"'
REPO_TARBALL=https://github.com/gansger/aggentnik/archive/refs/heads/main.tar.gz
APP_DIR=/opt/aggentnik
STATE_DIR=/var/lib/malinnik
DATA_DIR="$STATE_DIR/data"
PANEL_UID=7700
INTERNAL_PORT=7699
DEFAULT_PORT=7700
OLD_NGINX_CONF=/etc/nginx/conf.d/malinnik.conf
LOG_FILE=/var/log/aggentnik-install.log

# --- функции ---

die() { printf '\nОшибка: %s\n' "$1" >&2; exit 1; }

# Подробный вывод команд идёт в журнал, а на экране только этапы.
format_time() { printf '%d:%02d' "$(($1 / 60))" "$(($1 % 60))"; }

try_step() {  # try_step "Название" команда...
  local title="$1"
  shift
  local started=$SECONDS frames='|/-\' i=0 code=0
  printf '\n--- %s: %s\n' "$(date '+%F %T')" "$title" >>"$LOG_FILE"
  "$@" >>"$LOG_FILE" 2>&1 </dev/null &
  local pid=$!
  if [ -t 1 ]; then
    # Только возврат каретки: управление курсором в некоторых терминалах даёт повторы строк.
    while kill -0 "$pid" 2>/dev/null; do
      printf '\r  %s  %s  %s   ' "${frames:$((i++ % 4)):1}" "$title" "$(format_time $((SECONDS - started)))"
      sleep 0.25
    done
    printf '\r%*s\r' "$((${#title} + 24))" ''
  else
    printf '  ...  %s\n' "$title"
  fi
  wait "$pid" || code=$?
  if [ "$code" -eq 0 ]; then
    printf '  OK   %s  (%s)\n' "$title" "$(format_time $((SECONDS - started)))"
  else
    printf '  ОШИБКА  %s\n' "$title"
  fi
  return "$code"
}

step() {
  local title="$1"
  if ! try_step "$@"; then
    printf '\nПоследние строки журнала:\n' >&2
    tail -n 20 "$LOG_FILE" >&2 || true
    printf '\nВесь журнал: %s\n' "$LOG_FILE" >&2
    die "не удалось выполнить этап «$title»."
  fi
}

require_terminal() {
  if [ ! -t 0 ]; then
    die "установщику нужно задать вопросы, а ввод с клавиатуры недоступен.
Запусти его на самом сервере так:

  $INSTALL_COMMAND"
  fi
}

ask() {  # ask "вопрос" "значение по умолчанию". Вопрос идёт в stderr: stdout отдаёт ответ.
  local answer
  if [ -n "$2" ]; then
    printf '%s [%s]: ' "$1" "$2" >&2
  else
    printf '%s: ' "$1" >&2
  fi
  IFS= read -r answer || die "не удалось прочитать ответ. Запусти так: $INSTALL_COMMAND"
  answer="${answer#"${answer%%[![:space:]]*}"}"
  answer="${answer%"${answer##*[![:space:]]}"}"
  printf '%s' "${answer:-$2}"
}

confirm() {  # confirm "вопрос" y|n
  local hint answer
  if [ "$2" = y ]; then hint="Д/н"; else hint="д/Н"; fi
  printf '%s [%s]: ' "$1" "$hint" >&2
  IFS= read -r answer || die "не удалось прочитать ответ. Запусти так: $INSTALL_COMMAND"
  answer="${answer%$'\r'}"  # некоторые веб-консоли присылают и этот символ
  case "${answer:-$2}" in
    y|Y|yes|д|Д|да|Да) return 0 ;;
    *) return 1 ;;
  esac
}

env_value() {  # читает значение из прошлой установки
  [ -f "$APP_DIR/.env" ] || return 0
  sed -n "s/^$1=//p" "$APP_DIR/.env" | tail -n 1
}

normalize_domain() {
  local domain
  domain="$(printf '%s' "$1" | tr 'A-Z' 'a-z')"
  domain="${domain%.}"
  # Кириллические домены вроде сайт.рф сертификат принимает только в виде xn--...
  if ! printf '%s' "$domain" | LC_ALL=C grep -q '^[a-z0-9.-]*$'; then
    domain="$(python3 -c 'import sys; print(sys.argv[1].encode("idna").decode())' "$domain" 2>/dev/null || true)"
  fi
  if [[ "$domain" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]{2,}$ ]]; then
    printf '%s' "$domain"
  fi
}

valid_port() {
  [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1024 ] && [ "$1" -le 65535 ] \
    && [ "$1" -ne "$INTERNAL_PORT" ] && { [ "$1" -lt 8100 ] || [ "$1" -gt 8199 ]; }
}

port_owner() {
  ss -ltnpH "sport = :$1" 2>/dev/null || true
}

is_vpn_device() {
  [[ "$1" =~ ^(wg|awg|amn|tun|tap|malinnik|nordlynx|proton) ]]
}

outbound_device() {
  ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n 1
}

local_address() {
  hostname -I 2>/dev/null | awk '{print $1}'
}

caddyfile() {  # caddyfile домен порт почта
  cat <<EOF
{
	# Панель и сайты делят сеть сервера, и порт управления был бы открыт для любого сайта.
	admin off
	https_port $2
$( [ -z "$3" ] || printf '\temail %s\n' "$3" )
}

$1 {
	reverse_proxy 127.0.0.1:$INTERNAL_PORT {
		header_up X-Real-IP {remote_host}
	}
}
EOF
}

download_project() {  # download_project папка
  curl -fsSL "$REPO_TARBALL" | tar -xz --strip-components=1 -C "$1"
}

install_docker() {
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
}

disable_old_install() {
  if command -v awg-quick >/dev/null 2>&1 && [ -e /sys/class/net/malinnik0 ]; then
    awg-quick down /etc/amnezia/amneziawg/malinnik0.conf || true
  fi
  systemctl disable --now malinnik >/dev/null 2>&1 || true
  systemctl disable --now malinnik-vpn >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/malinnik.service.d/https.conf
  systemctl daemon-reload
  if [ -f "$OLD_NGINX_CONF" ]; then
    # Файл сохраняется: nginx читает только *.conf, а вернуть настройку можно переименованием.
    mv "$OLD_NGINX_CONF" "$OLD_NGINX_CONF.disabled"
    local other_sites certificates
    other_sites="$(find /etc/nginx/conf.d /etc/nginx/sites-enabled -maxdepth 1 \( -name '*.conf' -o -type l \) 2>/dev/null | wc -l)"
    if [ "$other_sites" -eq 0 ]; then
      systemctl disable --now nginx || true
    else
      systemctl reload nginx || true
    fi
    certificates="$(find /etc/letsencrypt/renewal -maxdepth 1 -name '*.conf' 2>/dev/null | wc -l)"
    if [ "$certificates" -le 1 ]; then
      systemctl disable --now certbot.timer || true
    fi
  fi
}

remove_old_vpn_container() {
  # VPN в контейнере больше нет. Сначала его выключаем, чтобы не осталось правил маршрутизации.
  if docker ps --format '{{.Names}}' | grep -q '^aggentnik-vpn-1$'; then
    docker exec aggentnik-vpn-1 awg-quick down /etc/amnezia/amneziawg/malinnik0.conf || true
  fi
}

start_services() {
  remove_old_vpn_container
  [ -n "$DOMAIN" ] || docker compose --profile domain rm -fsv caddy || true
  docker compose up -d --remove-orphans
  docker volume rm aggentnik_vpn-socket >/dev/null 2>&1 || true
  # Порт управления Caddy выключен, поэтому новый Caddyfile применяется только перезапуском.
  [ -z "$DOMAIN" ] || docker compose restart caddy
  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow "$PUBLIC_PORT/tcp"
    ufw allow 8100:8199/tcp
    [ -z "$DOMAIN" ] || ufw allow 80/tcp
  fi
}

panel_answers() {
  if [ -n "$DOMAIN" ]; then
    curl -fsS -o /dev/null --max-time 5 --resolve "$DOMAIN:$PUBLIC_PORT:127.0.0.1" "https://$DOMAIN:$PUBLIC_PORT/"
  else
    curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PUBLIC_PORT/"
  fi
}

wait_for_panel() {
  local attempt
  for attempt in $(seq 1 36); do
    panel_answers && return 0
    sleep 5
  done
  return 1
}

port_from_outside() {  # port_from_outside домен порт: open, closed или unknown
  local response id result="" attempt
  response="$(curl -fsS --max-time 15 -H 'Accept: application/json' \
    "https://check-host.net/check-tcp?host=$1:$2&max_nodes=3" 2>/dev/null)" || { echo unknown; return 0; }
  id="$(printf '%s' "$response" | sed -n 's/.*"request_id":"\([^"]*\)".*/\1/p')"
  [ -n "$id" ] || { echo unknown; return 0; }
  # Сервис отвечает примерно за минуту. Пока проверка идёт, у узлов в ответе стоит null.
  for attempt in $(seq 1 18); do
    sleep 5
    result="$(curl -fsS --max-time 15 -H 'Accept: application/json' \
      "https://check-host.net/check-result/$id" 2>/dev/null || true)"
    if printf '%s' "$result" | grep -q '"time"'; then
      break
    fi
    if [ -n "$result" ] && ! printf '%s' "$result" | grep -q 'null'; then
      break
    fi
  done
  if printf '%s' "$result" | grep -q '"time"'; then
    echo open
  elif printf '%s' "$result" | grep -q '"error"'; then
    echo closed
  else
    echo unknown
  fi
}

describe_port() {  # describe_port порт состояние
  case "$2" in
    open) printf '  Порт %s из интернета: открыт\n' "$1" ;;
    closed) printf '  Порт %s из интернета: ЗАКРЫТ\n' "$1" ;;
    *) printf '  Порт %s из интернета: проверить не удалось\n' "$1" ;;
  esac
}

diagnose_certificate() {
  local address_v4 address_v6 public_ip device acme_error port80 port_panel local_ip
  address_v4="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
  address_v6="$( (getent ahostsv6 "$DOMAIN" 2>/dev/null || true) | awk '{print $1}' | grep -v '^::ffff:' | head -n 1 || true)"
  public_ip="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  device="$(outbound_device)"
  local_ip="$(local_address)"

  echo
  echo "Сертификат пока не получен, поэтому панель по домену не открывается."
  echo "Caddy продолжает попытки сам: как только причина будет устранена, панель заработает."
  echo
  echo "Проверяю, в чём дело. Порты проверяются извне через check-host.net, это займёт около минуты."
  echo "  Домен $DOMAIN указывает на: ${address_v4:-никуда}"
  echo "  Внешний адрес сервера: ${public_ip:-определить не удалось}"
  if curl -fsS -o /dev/null --max-time 10 https://acme-v02.api.letsencrypt.org/directory 2>/dev/null; then
    echo "  Сервер Let's Encrypt с этой машины: доступен"
  else
    echo "  Сервер Let's Encrypt с этой машины: НЕДОСТУПЕН"
  fi
  local results
  results="$(mktemp -d)"
  port_from_outside "$DOMAIN" 80 >"$results/80" &
  port_from_outside "$DOMAIN" "$PUBLIC_PORT" >"$results/panel" &
  wait
  port80="$(cat "$results/80")"
  port_panel="$(cat "$results/panel")"
  rm -rf "$results"
  describe_port 80 "$port80"
  describe_port "$PUBLIC_PORT" "$port_panel"
  acme_error="$(docker compose logs --no-log-prefix --tail 200 caddy 2>/dev/null \
    | grep -oE 'urn:ietf:params:acme:error:[A-Za-z]+[^"]*' | tail -n 1 || true)"
  [ -z "$acme_error" ] || echo "  Ответ Let's Encrypt: $acme_error"

  echo
  echo "Что сделать:"
  if [ -z "$address_v4" ]; then
    echo "  - Создай у регистратора A-запись $DOMAIN на ${public_ip:-внешний адрес сервера}."
  elif [ -n "$public_ip" ] && [ "$address_v4" != "$public_ip" ] && ! is_vpn_device "$device"; then
    echo "  - A-запись домена ведёт на $address_v4, а внешний адрес сервера $public_ip. Исправь запись."
  fi
  if [ -n "$address_v6" ]; then
    echo "  - У домена есть AAAA-запись $address_v6. Let's Encrypt сначала идёт по IPv6. Если этот адрес не ведёт на сервер, удали запись."
  fi
  if is_vpn_device "$device"; then
    echo "  - Начни с этого: трафик сервера идёт через VPN ($device), поэтому внешний адрес выше — это адрес VPN. Ответы на входящие соединения уходят в туннель, и порты выглядят закрытыми, даже если проброс настроен верно."
    echo "    Чтобы оставить VPN и принимать соединения: $KEEP_INBOUND_COMMAND"
    echo "    Затем: cd $APP_DIR && sudo docker compose restart caddy"
  fi
  if [ "$port80" != open ]; then
    echo "  - Открой порт 80. Разрешения на самом сервере мало: на роутере нужен проброс порта 80 на ${local_ip:-адрес сервера}."
    echo "  - Если проброс есть, а порт закрыт, адрес ${public_ip:-сервера} может не быть адресом роутера (CGNAT), либо провайдер блокирует порт. Сверь с адресом WAN в настройках роутера и уточни у провайдера."
    echo "  - Если закрыт только порт 80, сертификат можно получить и через 443: на роутере проброс внешнего порта 443 на ${local_ip:-адрес сервера}:$PUBLIC_PORT."
  fi
  if [ "$port_panel" != open ]; then
    echo "  - Открой порт $PUBLIC_PORT: на роутере нужен проброс на ${local_ip:-адрес сервера}."
  fi
  echo
  echo "Журнал сертификата: cd $APP_DIR && sudo docker compose logs -f caddy"
  echo "Чтобы пока открыть панель без домена, запусти установщик снова и оставь домен пустым."
}

# --- установка ---

require_terminal
[ "$(id -u)" -eq 0 ] || die "нужны права администратора. Запусти так: $INSTALL_COMMAND"
: >>"$LOG_FILE"

echo
echo "Аггентник: установка"
echo

previous_domain="$(env_value DOMAIN)"
DOMAIN=""
while :; do
  answer="$(ask "Домен панели, например panel.example.ru" "$previous_domain")"
  if [ -z "$answer" ]; then
    echo "Без домена панель работает по HTTP без шифрования. Такой вариант подходит только для домашней сети."
    confirm "Продолжить без домена?" n && break
    continue
  fi
  DOMAIN="$(normalize_domain "$answer")"
  [ -n "$DOMAIN" ] && break
  echo "Это не похоже на домен: $answer"
done

EMAIL=""
if [ -n "$DOMAIN" ]; then
  while :; do
    EMAIL="$(ask "Почта для Let's Encrypt (можно оставить пустой)" "$(env_value ACME_EMAIL)")"
    if [ -z "$EMAIL" ] || [[ "$EMAIL" =~ ^[^[:space:]@\{\}]+@[^[:space:]@\{\}]+\.[^[:space:]@\{\}]+$ ]]; then
      break
    fi
    echo "Это не похоже на почту: $EMAIL"
  done
fi

previous_port="$(env_value PUBLIC_PORT)"
while :; do
  PUBLIC_PORT="$(ask "Порт панели" "${previous_port:-$DEFAULT_PORT}")"
  valid_port "$PUBLIC_PORT" && break
  echo "Нужен порт от 1024 до 65535, кроме $INTERNAL_PORT и 8100-8199."
done

OLD_INSTALL=no
if systemctl cat malinnik >/dev/null 2>&1 && systemctl is-enabled --quiet malinnik 2>/dev/null; then
  OLD_INSTALL=yes
  echo
  echo "Найдена установка без Docker. Сайты, пароль и настройки останутся, а её службы будут отключены."
  echo "Сайты с Python и зависимостями Node.js потребуют кнопки «Пересобрать»."
  confirm "Перенести её в Docker?" y || die "установка отменена, ничего не изменено."
fi

if [ -n "$DOMAIN" ]; then
  url="https://$DOMAIN:$PUBLIC_PORT"
else
  url="http://$(local_address):$PUBLIC_PORT"
fi
echo
echo "Панель будет доступна по адресу: $url"
if [ -n "$DOMAIN" ]; then
  echo "Для сертификата на роутере нужны пробросы портов 80 и $PUBLIC_PORT на $(local_address)."
fi
confirm "Начать установку?" y || die "установка отменена, ничего не изменено."
echo

command -v curl >/dev/null 2>&1 || step "Устанавливаю curl" sh -c 'apt-get update -qq && apt-get install -y -qq curl'

if [ -n "$DOMAIN" ]; then
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
  public_ip="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  device="$(outbound_device)"
  if [ -z "$resolved" ]; then
    echo "Домен $DOMAIN пока никуда не указывает. Создай A-запись на ${public_ip:-внешний адрес сервера}."
    confirm "Всё равно продолжить?" n || die "установка отменена, ничего не изменено."
  elif is_vpn_device "$device" && ! ip rule show 2>/dev/null | grep -qF 'fwmark 0x7700'; then
    echo "Трафик сервера идёт через VPN ($device): Let's Encrypt не сможет получить ответ от сервера."
    echo "Чтобы оставить VPN и принимать соединения, сначала выполни: $KEEP_INBOUND_COMMAND"
    confirm "Всё равно продолжить?" n || die "установка отменена, ничего не изменено."
  elif [ -n "$public_ip" ] && [ "$resolved" != "$public_ip" ]; then
    echo "Домен указывает на $resolved, а внешний адрес сервера $public_ip."
    confirm "Всё равно продолжить?" n || die "установка отменена, ничего не изменено."
  fi

  owner="$(port_owner 80)"
  if [ -n "$owner" ] && ! grep -q caddy <<<"$owner" \
      && ! { [ -f "$OLD_NGINX_CONF" ] && grep -q nginx <<<"$owner"; }; then
    die "порт 80 занят другой программой, а он нужен для сертификата:
$owner"
  fi
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не установлен. Его поставит официальный скрипт get.docker.com."
  confirm "Поставить Docker?" y || die "без Docker установка невозможна."
  step "Устанавливаю Docker" install_docker
fi
docker compose version >/dev/null 2>&1 \
  || die "нет Docker Compose. Поставь его: sudo apt-get install docker-compose-plugin"

script_path="${BASH_SOURCE[0]:-}"
source_dir=""
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  candidate="$(cd "$(dirname "$script_path")" && pwd)"
  [ -f "$candidate/Dockerfile" ] && source_dir="$candidate"
fi
if [ -z "$source_dir" ]; then
  source_dir="$(mktemp -d)"
  trap 'rm -rf "$source_dir"' EXIT
  step "Скачиваю проект" download_project "$source_dir"
fi
install -d -m 755 "$APP_DIR"
if [ "$source_dir" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR/malinnik"
  cp -r "$source_dir/malinnik" "$source_dir/Dockerfile" "$source_dir/docker-compose.yml" \
    "$source_dir/.dockerignore" "$APP_DIR/"
fi

if [ -n "$DOMAIN" ]; then
  profiles=domain
  panel_host=127.0.0.1
  panel_port="$INTERNAL_PORT"
  caddyfile "$DOMAIN" "$PUBLIC_PORT" "$EMAIL" > "$APP_DIR/Caddyfile"
else
  profiles=""
  panel_host=0.0.0.0
  panel_port="$PUBLIC_PORT"
fi
cat > "$APP_DIR/.env" <<EOF
COMPOSE_PROJECT_NAME=aggentnik
COMPOSE_PROFILES=$profiles
PANEL_HOST=$panel_host
PANEL_PORT=$panel_port
PUBLIC_PORT=$PUBLIC_PORT
DOMAIN=$DOMAIN
ACME_EMAIL=$EMAIL
EOF
chmod 600 "$APP_DIR/.env"
cd "$APP_DIR"

# Сборка идёт до остановки старой установки: если она упадёт, панель продолжит работать.
step "Собираю образ панели (первый раз до 10 минут)" docker compose build panel
[ -z "$DOMAIN" ] || step "Скачиваю Caddy для сертификата" docker compose pull caddy

ROLLBACK_HINT=""
if [ "$OLD_INSTALL" = yes ]; then
  step "Отключаю установку без Docker" disable_old_install
  ROLLBACK_HINT="Вернуться на установку без Docker:
  cd $APP_DIR && sudo docker compose down
  sudo chown -R malinnik:malinnik $STATE_DIR
  sudo systemctl enable --now malinnik"
fi

if [ -n "$DOMAIN" ]; then
  owner="$(port_owner 80)"
  if [ -n "$owner" ] && ! grep -q caddy <<<"$owner"; then
    [ -z "$ROLLBACK_HINT" ] || echo "$ROLLBACK_HINT" >&2
    die "порт 80 всё ещё занят:
$owner"
  fi
fi

install -d -m 750 "$STATE_DIR" "$DATA_DIR"
chown -R "$PANEL_UID:$PANEL_UID" "$STATE_DIR"

PASSWORD=""
if [ ! -f "$DATA_DIR/config.json" ]; then
  PASSWORD="$(docker compose run --rm -T --no-deps panel \
    python3 -m malinnik --data "$DATA_DIR" set-password --generate 2>>"$LOG_FILE" | tail -n 1 | tr -d '\r')"
  [ -n "$PASSWORD" ] || die "не удалось создать пароль. Журнал: $LOG_FILE"
fi

step "Запускаю панель" start_services

if [ -n "$DOMAIN" ]; then
  wait_title="Получаю сертификат (до 3 минут)"
else
  wait_title="Жду ответа панели"
fi
ok=""
if try_step "$wait_title" wait_for_panel; then
  ok=1
fi

echo
if [ -n "$ok" ]; then
  echo "Готово. Панель: $url"
elif [ -n "$DOMAIN" ]; then
  diagnose_certificate
else
  echo "Панель пока не отвечает. Журнал: cd $APP_DIR && sudo docker compose logs -f panel"
fi
if [ -n "$PASSWORD" ]; then
  echo
  echo "Пароль: $PASSWORD"
  echo "Сохрани его: повторно он не показывается."
fi
if [ "$OLD_INSTALL" = yes ]; then
  echo
  echo "Сайты с Python и зависимостями Node.js нажми «Пересобрать»: их файлы собраны вне контейнера."
  echo "$ROLLBACK_HINT"
fi
[ -n "$ok" ] || exit 1
