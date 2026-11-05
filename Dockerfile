# Один образ на два контейнера: панель и сервис VPN (см. docker-compose.yml).

# --- сборка AmneziaWG ---
# amneziawg-go работает без модуля ядра: контейнеру не нужны исходники ядра малины.
FROM golang:1-trixie AS awg
ARG AWG_TOOLS_TAG=v3.1.20260812
RUN git clone --depth 1 --branch "$AWG_TOOLS_TAG" https://github.com/amnezia-vpn/amneziawg-tools.git /src/tools \
    && make -C /src/tools/src -j"$(nproc)" \
    && make -C /src/tools/src install DESTDIR=/out PREFIX=/usr/local \
        WITH_WGQUICK=yes WITH_BASHCOMPLETION=no WITH_SYSTEMDUNITS=no \
    && git clone --depth 1 https://github.com/amnezia-vpn/amneziawg-go.git /src/go \
    && make -C /src/go \
    && install -m 755 /src/go/amneziawg-go /out/usr/local/bin/amneziawg-go

# --- образ панели ---
FROM debian:trixie-slim

# Среды для загружаемых сайтов и всё, что нужно awg-quick. build-essential нужен пакетам,
# которые собираются при установке: под ARM готовых сборок часто нет.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl bash procps iproute2 nftables \
        python3 python3-venv python3-dev build-essential \
        nodejs npm php-cli 7zip \
    && curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg \
    && echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
        > /etc/apt/sources.list.d/cloudflared.list \
    && sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends cloudflared \
    && { apt-get install -y --no-install-recommends 7zip-rar || echo "RAR недоступен, остальные форматы работают"; } \
    && rm -rf /var/lib/apt/lists/*

COPY --from=awg /out/usr/local/bin/ /usr/local/bin/
COPY malinnik /opt/malinnik/malinnik

# Пути те же, что при установке без Docker: в данных сайтов записаны абсолютные пути.
RUN groupadd --gid 7700 malinnik \
    && useradd --uid 7700 --gid 7700 --home-dir /var/lib/malinnik --shell /usr/sbin/nologin malinnik \
    && install -d -o malinnik -g malinnik -m 750 /var/lib/malinnik /var/lib/malinnik/data \
    && install -d -m 755 /run/malinnik-vpn

ENV HOME=/var/lib/malinnik \
    MALINNIK_DATA=/var/lib/malinnik/data \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8
WORKDIR /opt/malinnik
USER malinnik
CMD ["python3", "-m", "malinnik", "serve"]
