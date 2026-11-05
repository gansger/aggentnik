#!/usr/bin/env bash
# Собирает AmneziaWG из официальных исходников. Запускается из install.sh.
#
# Используется amneziawg-go: ему не нужен модуль ядра, для сборки которого на малине
# потребовались бы полные исходники ядра, и он не ломается при обновлении ядра.
# Занимает несколько минут и выполняется один раз.
set -euo pipefail

TOOLS_TAG=v3.1.20260812

case "$(dpkg --print-architecture)" in
  arm64) GO_ARCH=arm64 ;;
  armhf) GO_ARCH=armv6l ;;
  amd64) GO_ARCH=amd64 ;;
  *) echo "Неизвестная архитектура: $(dpkg --print-architecture)" >&2; exit 1 ;;
esac

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "    Сборочные пакеты"
apt-get install -y -qq build-essential git curl iproute2 nftables

echo "    amneziawg-tools $TOOLS_TAG"
git clone -q --depth 1 --branch "$TOOLS_TAG" https://github.com/amnezia-vpn/amneziawg-tools.git "$BUILD_DIR/tools" 2>/dev/null
make -s -C "$BUILD_DIR/tools/src" -j"$(nproc)"
make -s -C "$BUILD_DIR/tools/src" install PREFIX=/usr/local \
  WITH_WGQUICK=yes WITH_BASHCOMPLETION=no WITH_SYSTEMDUNITS=no

# В Debian Go старее, чем требует amneziawg-go, поэтому берём официальную сборку с проверкой
# контрольной суммы. После установки компилятор удаляется вместе с временной папкой.
echo "    Компилятор Go"
read -r GO_FILE GO_SHA < <(curl -fsSL 'https://go.dev/dl/?mode=json' | python3 -c '
import json, sys
arch = sys.argv[1]
for release in json.load(sys.stdin):
    for item in release["files"]:
        if item["os"] == "linux" and item["arch"] == arch and item["kind"] == "archive":
            print(item["filename"], item["sha256"])
            sys.exit()
sys.exit("Go для этой архитектуры не найден")
' "$GO_ARCH")
curl -fsSL "https://go.dev/dl/$GO_FILE" -o "$BUILD_DIR/$GO_FILE"
echo "$GO_SHA  $BUILD_DIR/$GO_FILE" | sha256sum -c --quiet
tar -xzf "$BUILD_DIR/$GO_FILE" -C "$BUILD_DIR"

echo "    amneziawg-go"
git clone -q --depth 1 https://github.com/amnezia-vpn/amneziawg-go.git "$BUILD_DIR/go-src" 2>/dev/null
(
  cd "$BUILD_DIR/go-src"
  PATH="$BUILD_DIR/go/bin:$PATH" GOTOOLCHAIN=local GOPATH="$BUILD_DIR/gopath" \
    GOCACHE="$BUILD_DIR/gocache" GOFLAGS=-modcacherw make -s
)
install -m 755 "$BUILD_DIR/go-src/amneziawg-go" /usr/local/bin/amneziawg-go
