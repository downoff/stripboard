#!/bin/sh
# Start ClickHouse, wait for it to answer, then start the app.
#
# The wait is not optional. Cloud Run sends traffic the moment the port is open,
# and the first request runs the pipeline, which writes to ClickHouse — starting
# uvicorn before the server is up turns the first page load into a 500.
#
# NOT --daemon. Daemonizing forks, detaches and writes to a log file; inside a
# container that child can die without the parent noticing and without the log
# ever being created, which is exactly what happened on Cloud Run on 2026-08-02
# ("no clickhouse err log written at all"). Running it as a plain background job
# keeps it a child of this shell and sends its output to stdout, where Cloud Run
# will actually show it.

clickhouse-server --config-file=/etc/clickhouse-server/config.xml &
CH_PID=$!

echo "waiting for clickhouse (pid $CH_PID)"
i=0
while [ $i -lt 180 ]; do
    if curl -sf "http://127.0.0.1:8123/ping" >/dev/null 2>/dev/null; then
        echo "clickhouse up after ${i}s"
        break
    fi
    if ! kill -0 "$CH_PID" 2>/dev/null; then
        echo "FAILED — clickhouse process died after ${i}s (see its output above)" >&2
        exit 1
    fi
    i=$((i + 1))
    sleep 1
done

if [ $i -ge 180 ]; then
    echo "FAILED — clickhouse alive but not answering /ping after 180s" >&2
    exit 1
fi

exec uvicorn stripboard.web:app --host 0.0.0.0 --port "${PORT:-8080}"
