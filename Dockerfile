# One image, two processes: a real ClickHouse server and the app that talks to it
# through the official mcp-clickhouse tool functions.
#
# Co-locating them is deliberate. The contest requires ClickHouse in the running
# system, and a hosted demo that quietly swapped in SQLite would not be the thing
# it claims to be. Data here is per-instance and disposable, which is correct — the
# element table is rebuilt from the screenplay on every run.
#
# Python is the base and ClickHouse is installed on top, rather than the other way
# round. The clickhouse-server images are pinned to older Ubuntu releases (24.8 is
# 20.04 / Python 3.8), and this project needs 3.11+.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key \
      | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" \
      > /etc/apt/sources.list.d/clickhouse.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends clickhouse-server clickhouse-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY fixtures ./fixtures
RUN pip install --no-cache-dir . fastapi uvicorn python-multipart

# Small-instance ClickHouse. The defaults assume a database host with tens of GB;
# this is a demo container holding a few hundred rows.
RUN printf '%s\n' \
    '<clickhouse>' \
    '  <listen_host>127.0.0.1</listen_host>' \
    '  <max_server_memory_usage_to_ram_ratio>0.5</max_server_memory_usage_to_ram_ratio>' \
    '  <mark_cache_size>134217728</mark_cache_size>' \
    '  <logger><level>warning</level><console>1</console></logger>' \
    '</clickhouse>' > /etc/clickhouse-server/config.d/demo.xml

# ClickHouse refuses to start when the process user does not own the data dir
# (MISMATCHING_USERS_FOR_PROCESS_AND_DATA). Cloud Run runs the container as root,
# so hand root the data rather than adding a su layer to the entrypoint.
RUN mkdir -p /var/lib/clickhouse /var/log/clickhouse-server \
 && chown -R root:root /var/lib/clickhouse /var/log/clickhouse-server

ENV CLICKHOUSE_HOST=127.0.0.1 \
    CLICKHOUSE_PORT=8123 \
    CLICKHOUSE_USER=default \
    CLICKHOUSE_PASSWORD="" \
    CLICKHOUSE_SECURE=false \
    CLICKHOUSE_VERIFY=false \
    PORT=8080

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
