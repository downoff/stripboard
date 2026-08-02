#!/bin/sh
# Start ClickHouse, wait for it to answer, then start the app.
#
# The wait is not optional. Cloud Run sends traffic the moment the port is open,
# and the first request runs the pipeline, which writes to ClickHouse — starting
# uvicorn before the server is up turns the first page load into a 500.
set -e

clickhouse-server --config-file=/etc/clickhouse-server/config.xml --daemon

printf 'waiting for clickhouse'
i=0
while [ $i -lt 60 ]; do
    if curl -sf "http://127.0.0.1:8123/ping" >/dev/null 2>/dev/null; then
        echo " up"
        break
    fi
    printf '.'
    i=$((i + 1))
    sleep 1
done

if [ $i -ge 60 ]; then
    echo " FAILED — clickhouse did not answer in 60s" >&2
    exit 1
fi

exec uvicorn stripboard.web:app --host 0.0.0.0 --port "${PORT:-8080}"
