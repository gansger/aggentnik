FROM debian:trixie-slim

# Среды для загружаемых сайтов. build-essential нужен пакетам, которые собираются при
# установке: под ARM готовых сборок часто нет.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl \
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

COPY malinnik /opt/malinnik/malinnik

# Пути те же, что в прежних установках: в данных сайтов записаны абсолютные пути.
RUN groupadd --gid 7700 malinnik \
    && useradd --uid 7700 --gid 7700 --home-dir /var/lib/malinnik --shell /usr/sbin/nologin malinnik \
    && install -d -o malinnik -g malinnik -m 750 /var/lib/malinnik /var/lib/malinnik/data

ENV HOME=/var/lib/malinnik \
    MALINNIK_DATA=/var/lib/malinnik/data \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8
WORKDIR /opt/malinnik
USER malinnik
CMD ["python3", "-m", "malinnik", "serve"]
