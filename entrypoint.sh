#!/bin/sh
# Start ClickHouse, wait for it to answer, then start the app.
#
# The wait is not optional. Cloud Run sends traffic the moment the port is open,
# and the first request runs the pipeline, which writes to ClickHouse — starting
# uvicorn before the server is up turns the first page load into a 500.
#
# 180s, not 60s: ClickHouse comes up in ~5s on a laptop but needs far longer on a
# cold Cloud Run instance, where the filesystem is an overlay and the first boot
# also builds the system tables. 60s failed the deploy on 2026-08-02.
set -e

clickhouse-server --config-file=/etc/clickhouse-server/config.xml --daemon

echo "waiting for clickhouse"
i=0
while [ $i -lt 180 ]; do
    if curl -sf "http://127.0.0.1:8123/ping" >/dev/null 2>/dev/null; then
        echo "clickhouse up after ${i}s"
        break
    fi
    i=$((i + 1))
    sleep 1
done

if [ $i -ge 180 ]; then
    echo "FAILED — clickhouse did not answer in 180s. Its own log follows:" >&2
    tail -40 /var/log/clickhouse-server/clickhouse-server.err.log >&2 2>/dev/null \
        || echo "(no clickhouse err log written at all)" >&2
    exit 1
fi

exec uvicorn stripboard.web:app --host 0.0.0.0 --port "${PORT:-8080}"
