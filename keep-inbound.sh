#!/usr/bin/env bash
# Позволяет серверу принимать соединения из интернета при включённом VPN.
#
#   Включить:  sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/gansger/aggentnik/main/keep-inbound.sh)"
#   Выключить: sudo /usr/local/sbin/aggentnik-inbound remove
#
# VPN, который забирает весь трафик, не закрывает порты: запрос доходит до сервера через роутер,
# но ответ уходит в туннель и теряется. Эти правила отмечают соединения, пришедшие через роутер,
# и отправляют ответы на них обратно через роутер. Остальной трафик по-прежнему идёт через VPN.
# Правила работают независимо от VPN, им всё равно, включён он или нет.
set -euo pipefail

SERVICE=/etc/systemd/system/aggentnik-inbound.service
HELPER=/usr/local/sbin/aggentnik-inbound

if [ "$(id -u)" -ne 0 ]; then
  echo "Нужны права администратора: запусти команду через sudo." >&2
  exit 1
fi

if ! command -v nft >/dev/null 2>&1; then
  echo "Ставлю nftables..."
  apt-get update -qq >/dev/null
  apt-get install -y -qq nftables >/dev/null
fi

cat > "$HELPER" <<'EOF'
#!/bin/sh
# Создано keep-inbound.sh: ответы на входящие соединения идут через роутер, а не в VPN.
MARK=0x7700
TABLE=aggentnik_inbound

down() {
  nft delete table inet "$TABLE" 2>/dev/null || true
  while ip rule del fwmark "$MARK" lookup main 2>/dev/null; do :; done
}

up() {
  # VPN с полным туннелем добавляет свою таблицу, а основная остаётся нетронутой.
  # Поэтому маршрут по умолчанию в ней ведёт на роутер.
  dev="$(ip -4 route show table main default | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n 1)"
  if [ -z "$dev" ]; then
    echo "Нет маршрута на роутер, пока сеть не поднялась" >&2
    exit 1
  fi
  down
  nft -f - <<NFT
table inet $TABLE {
  chain prerouting {
    type filter hook prerouting priority mangle; policy accept;
    iifname "$dev" meta nfproto ipv4 meta l4proto tcp ct state new ct mark set $MARK
    ct mark $MARK meta mark set $MARK
  }
  chain output {
    type route hook output priority mangle; policy accept;
    ct mark $MARK meta mark set $MARK
  }
}
NFT
  ip rule add fwmark "$MARK" lookup main priority 100
  # Иначе ядро проверяет обратный маршрут без учёта отметки и может отбросить пакет.
  sysctl -q -w net.ipv4.conf.all.src_valid_mark=1
  echo "Входящие соединения через $dev отвечают через роутер"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  remove)
    systemctl disable --now aggentnik-inbound 2>/dev/null || true
    down
    rm -f /etc/systemd/system/aggentnik-inbound.service
    systemctl daemon-reload
    rm -f "$0"
    echo "Правила удалены"
    ;;
  *)
    echo "Использование: $0 up|down|remove" >&2
    exit 2
    ;;
esac
EOF
chmod 755 "$HELPER"

cat > "$SERVICE" <<EOF
[Unit]
Description=Aggentnik: replies to inbound connections bypass VPN
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$HELPER up
ExecStop=$HELPER down
Restart=on-failure
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable aggentnik-inbound >/dev/null 2>&1
systemctl restart aggentnik-inbound

echo
systemctl --no-pager --lines=0 status aggentnik-inbound | sed -n '1,3p'
ip rule show | grep -F 'fwmark 0x7700' || true
echo
echo "Готово. Правила будут включаться сами после перезагрузки."
echo "Выключить: sudo $HELPER remove"
