#!/usr/bin/env bash
# Установка и обновление Аггентника в Docker. Спрашивает домен и запускает панель на нём.
#
#   curl -fsSL https://raw.githubusercontent.com/gansger/aggentnik/main/install-docker.sh | sudo bash
#
# Повторный запуск обновляет панель или меняет домен. Сайты, пароль и настройки сохраняются.
set -euo pipefail

REPO_TARBALL=https://github.com/gansger/aggentnik/archive/refs/heads/main.tar.gz
APP_DIR=/opt/aggentnik
STATE_DIR=/var/lib/malinnik
DATA_DIR="$STATE_DIR/data"
PANEL_UID=7700
INTERNAL_PORT=7699
DEFAULT_PORT=7700
OLD_NGINX_CONF=/etc/nginx/conf.d/malinnik.conf

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\nОшибка: %s\n' "$1" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "нужны права администратора. Запусти через sudo."
fi
# При запуске через curl | bash стандартный ввод занят самим скриптом, поэтому вопросы идут в терминал.
if ! { : </dev/tty; } 2>/dev/null; then
  die "нужен терминал для вопросов. Подключись через ssh -t."
fi

ask() {  # ask "вопрос" "значение по умолчанию"
  local answer
  if [ -n "$2" ]; then
    printf '%s [%s]: ' "$1" "$2" >/dev/tty
  else
    printf '%s: ' "$1" >/dev/tty
  fi
  IFS= read -r answer </dev/tty || answer=""
  answer="${answer#"${answer%%[![:space:]]*}"}"
  answer="${answer%"${answer##*[![:space:]]}"}"
  printf '%s' "${answer:-$2}"
}

confirm() {  # confirm "вопрос" y|n
  local hint answer
  if [ "$2" = y ]; then hint="Y/n"; else hint="y/N"; fi
  printf '%s [%s]: ' "$1" "$hint" >/dev/tty
  IFS= read -r answer </dev/tty || answer=""
  case "${answer:-$2}" in
    y|Y|д|Д|yes|да) return 0 ;;
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

caddyfile() {  # caddyfile домен порт почта
  cat <<EOF
{
	# Панель и сайты делят сеть малины, и порт управления был бы открыт для любого сайта.
	admin off
	https_port $2
}

$1 {
	tls {
		issuer acme {
$( [ -n "$3" ] && printf '\t\t\temail %s\n' "$3" )
			# Проверка через порт 443 не сработает: он не проброшен. Остаётся порт 80.
			disable_tlsalpn_challenge
		}
	}
	reverse_proxy 127.0.0.1:$INTERNAL_PORT {
		header_up X-Real-IP {remote_host}
	}
}
EOF
}

# --- вопросы ---

echo
echo "Аггентник: установка"
echo

previous_domain="$(env_value DOMAIN)"
DOMAIN=""
while :; do
  answer="$(ask "Домен панели (пусто — без домена, только для домашней сети)" "$previous_domain")"
  [ -n "$answer" ] || break
  DOMAIN="$(normalize_domain "$answer")"
  [ -n "$DOMAIN" ] && break
  echo "Это не похоже на домен: $answer" >/dev/tty
done

EMAIL=""
if [ -n "$DOMAIN" ]; then
  while :; do
    EMAIL="$(ask "Почта для Let's Encrypt (можно оставить пустой)" "$(env_value ACME_EMAIL)")"
    if [ -z "$EMAIL" ] || [[ "$EMAIL" =~ ^[^[:space:]@\{\}]+@[^[:space:]@\{\}]+\.[^[:space:]@\{\}]+$ ]]; then
      break
    fi
    echo "Это не похоже на почту: $EMAIL" >/dev/tty
  done
fi

while :; do
  PUBLIC_PORT="$(ask "Порт панели" "$(env_value PUBLIC_PORT || true)")"
  PUBLIC_PORT="${PUBLIC_PORT:-$DEFAULT_PORT}"
  valid_port "$PUBLIC_PORT" && break
  echo "Нужен порт от 1024 до 65535, кроме $INTERNAL_PORT и 8100-8199." >/dev/tty
done

vpn_default=y
[ "$(env_value ENABLE_VPN)" = no ] && vpn_default=n
if confirm "Включить поддержку VPN AmneziaWG (отдельный контейнер с полными правами)?" "$vpn_default"; then
  ENABLE_VPN=yes
else
  ENABLE_VPN=no
fi

# --- проверки ---

command -v curl >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq curl; }

OLD_INSTALL=no
if systemctl cat malinnik >/dev/null 2>&1 && systemctl is-enabled --quiet malinnik 2>/dev/null; then
  OLD_INSTALL=yes
  echo
  echo "Найдена установка без Docker. Сайты, пароль и настройки останутся, а её службы будут отключены."
  echo "Адрес панели может измениться, сайты с Python и зависимостями потребуют кнопки «Пересобрать»."
  confirm "Перенести её в Docker?" y || die "установка отменена, ничего не изменено."
fi

if [ -n "$DOMAIN" ]; then
  say "Проверяю домен"
  resolved="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1 {print $1}' || true)"
  # Сервис видит только внешний адрес малины.
  public_ip="$(curl -4 -fsS --max-time 8 https://api.ipify.org 2>/dev/null || true)"
  if [ -z "$resolved" ]; then
    echo "Домен $DOMAIN пока никуда не указывает. Создай A-запись на ${public_ip:-внешний адрес малины}."
    confirm "Всё равно продолжить?" n || die "установка отменена, ничего не изменено."
  elif [ -n "$public_ip" ] && [ "$resolved" != "$public_ip" ]; then
    echo "Домен указывает на $resolved, а внешний адрес малины $public_ip."
    confirm "Всё равно продолжить?" n || die "установка отменена, ничего не изменено."
  else
    echo "Домен указывает на $resolved"
  fi

  owner="$(port_owner 80)"
  if [ -n "$owner" ] && ! grep -q caddy <<<"$owner" \
      && ! { [ -f "$OLD_NGINX_CONF" ] && grep -q nginx <<<"$owner"; }; then
    die "порт 80 занят другой программой, а он нужен для сертификата:
$owner"
  fi
fi

# --- Docker ---

if ! command -v docker >/dev/null 2>&1; then
  echo
  echo "Docker не установлен. Его поставит официальный скрипт get.docker.com."
  confirm "Поставить Docker?" y || die "без Docker установка невозможна."
  say "Ставлю Docker"
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 \
  || die "нет Docker Compose. Поставь его: apt-get install docker-compose-plugin"

# --- файлы проекта ---

say "Копирую файлы в $APP_DIR"
script_path="${BASH_SOURCE[0]:-}"
source_dir=""
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
  candidate="$(cd "$(dirname "$script_path")" && pwd)"
  [ -f "$candidate/Dockerfile" ] && source_dir="$candidate"
fi
download_dir=""
if [ -z "$source_dir" ]; then
  download_dir="$(mktemp -d)"
  trap 'rm -rf "$download_dir"' EXIT
  curl -fsSL "$REPO_TARBALL" | tar -xz --strip-components=1 -C "$download_dir" \
    || die "не удалось скачать проект. Репозиторий должен быть публичным."
  source_dir="$download_dir"
fi
install -d -m 755 "$APP_DIR"
if [ "$source_dir" != "$APP_DIR" ]; then
  rm -rf "$APP_DIR/malinnik"
  cp -r "$source_dir/malinnik" "$source_dir/Dockerfile" "$source_dir/docker-compose.yml" \
    "$source_dir/.dockerignore" "$APP_DIR/"
fi

profiles=""
[ -n "$DOMAIN" ] && profiles="domain"
[ "$ENABLE_VPN" = yes ] && profiles="${profiles:+$profiles,}vpn"
if [ -n "$DOMAIN" ]; then
  panel_host=127.0.0.1
  panel_port="$INTERNAL_PORT"
else
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
ENABLE_VPN=$ENABLE_VPN
EOF
chmod 600 "$APP_DIR/.env"
if [ -n "$DOMAIN" ]; then
  caddyfile "$DOMAIN" "$PUBLIC_PORT" "$EMAIL" > "$APP_DIR/Caddyfile"
fi
cd "$APP_DIR"

# Сборка идёт до остановки старой установки: если она упадёт, панель продолжит работать.
say "Собираю образ (первый раз на малине это занимает 10-20 минут)"
docker compose build panel

# --- перенос старой установки ---

ROLLBACK_HINT=""
if [ "$OLD_INSTALL" = yes ]; then
  say "Отключаю установку без Docker"
  if command -v awg-quick >/dev/null 2>&1 && [ -e /sys/class/net/malinnik0 ]; then
    awg-quick down /etc/amnezia/amneziawg/malinnik0.conf || true
  fi
  systemctl disable --now malinnik malinnik-vpn >/dev/null 2>&1 || systemctl disable --now malinnik >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/malinnik.service.d/https.conf
  systemctl daemon-reload
  if [ -f "$OLD_NGINX_CONF" ]; then
    # Файл сохраняется: nginx читает только *.conf, а вернуть настройку можно переименованием.
    mv "$OLD_NGINX_CONF" "$OLD_NGINX_CONF.disabled"
    other_sites="$(find /etc/nginx/conf.d /etc/nginx/sites-enabled -maxdepth 1 \( -name '*.conf' -o -type l \) 2>/dev/null | wc -l)"
    if [ "$other_sites" -eq 0 ]; then
      systemctl disable --now nginx >/dev/null 2>&1 || true
    else
      systemctl reload nginx || true
    fi
    certificates="$(find /etc/letsencrypt/renewal -maxdepth 1 -name '*.conf' 2>/dev/null | wc -l)"
    if [ "$certificates" -le 1 ]; then
      systemctl disable --now certbot.timer >/dev/null 2>&1 || true
    fi
  fi
  ROLLBACK_HINT="Вернуться на установку без Docker:
  cd $APP_DIR && docker compose down
  chown -R malinnik:malinnik $STATE_DIR
  systemctl enable --now malinnik malinnik-vpn"
fi

if [ -n "$DOMAIN" ]; then
  owner="$(port_owner 80)"
  if [ -n "$owner" ] && ! grep -q caddy <<<"$owner"; then
    [ -z "$ROLLBACK_HINT" ] || echo "$ROLLBACK_HINT" >&2
    die "порт 80 всё ещё занят:
$owner"
  fi
fi

# --- запуск ---

say "Запускаю"
install -d -m 750 "$STATE_DIR" "$DATA_DIR"
chown -R "$PANEL_UID:$PANEL_UID" "$STATE_DIR"
install -d -m 700 /etc/amnezia/amneziawg /var/lib/malinnik-vpn

PASSWORD=""
if [ ! -f "$DATA_DIR/config.json" ]; then
  PASSWORD="$(docker compose run --rm -T --no-deps panel \
    python3 -m malinnik --data "$DATA_DIR" set-password --generate | tail -n 1 | tr -d '\r')"
fi

# Отключённые в ответах службы надо убрать: их профиль больше не активен.
[ -n "$DOMAIN" ] || docker compose --profile domain rm -fsv caddy >/dev/null 2>&1 || true
[ "$ENABLE_VPN" = yes ] || docker compose --profile vpn rm -fs vpn >/dev/null 2>&1 || true

docker compose up -d --remove-orphans
# Порт управления Caddy выключен, поэтому новый Caddyfile применяется только перезапуском.
[ -z "$DOMAIN" ] || docker compose restart caddy >/dev/null

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow "$PUBLIC_PORT/tcp" >/dev/null
  ufw allow 8100:8199/tcp >/dev/null
  [ -z "$DOMAIN" ] || ufw allow 80/tcp >/dev/null
fi

# --- проверка ---

say "Проверяю панель"
if [ -n "$DOMAIN" ]; then
  url="https://$DOMAIN:$PUBLIC_PORT"
  check=(curl -fsS -o /dev/null --max-time 5 --resolve "$DOMAIN:$PUBLIC_PORT:127.0.0.1" "$url/")
else
  url="http://$(hostname -I | awk '{print $1}'):$PUBLIC_PORT"
  check=(curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:$PUBLIC_PORT/")
fi
ok=""
for _ in $(seq 1 24); do
  if "${check[@]}" 2>/dev/null; then
    ok=1
    break
  fi
  sleep 5
done

echo
if [ -z "$ok" ]; then
  echo "Панель пока не отвечает по адресу $url"
  if [ -n "$DOMAIN" ]; then
    docker compose logs --tail 15 caddy || true
    echo
    echo "Чаще всего дело в сертификате. Проверь:"
    echo "  1. A-запись домена $DOMAIN указывает на внешний адрес малины."
    echo "  2. На роутере проброшены порты 80 и $PUBLIC_PORT."
    echo "Caddy сам продолжит попытки, запускать установку заново не нужно."
    echo "Журнал: cd $APP_DIR && docker compose logs -f caddy"
  else
    docker compose logs --tail 15 panel || true
  fi
else
  echo "Готово. Панель: $url"
fi
if [ -n "$PASSWORD" ]; then
  echo "Пароль: $PASSWORD"
  echo "Сохрани его: повторно он не показывается."
fi
if [ -n "$DOMAIN" ]; then
  echo "На роутере должны быть проброшены порты 80 и $PUBLIC_PORT. Сертификат продлевается сам."
fi
if [ "$OLD_INSTALL" = yes ]; then
  echo
  echo "Сайты с Python и с собранными зависимостями Node.js нажми «Пересобрать»: их файлы собраны вне контейнера."
  echo "$ROLLBACK_HINT"
fi
[ -n "$ok" ] || exit 1
