"""Prove the contest-required integration is real, on this machine, right now.

The ClickHouse track requires ClickHouse to be used **at runtime via the official
mcp-clickhouse MCP server** — a README mention is explicitly not enough. So this
module deliberately reaches ClickHouse *only* through `mcp_clickhouse`'s own tool
functions (`list_databases`, `run_query`), never through a raw driver, for every
read it performs.

Run it:  python -m stripboard.verify_stack
"""

from __future__ import annotations

import json
import os
import sys

# Default to the docker-compose credentials so `docker compose up -d` followed by
# this module just works. A real environment still wins over every default.
for _k, _v in {
    "CLICKHOUSE_HOST": "localhost",
    "CLICKHOUSE_PORT": "8123",
    "CLICKHOUSE_USER": "stripboard",
    "CLICKHOUSE_PASSWORD": "stripboard",
    "CLICKHOUSE_SECURE": "false",
}.items():
    os.environ.setdefault(_k, _v)

from mcp_clickhouse import create_clickhouse_client, list_databases, run_query

from stripboard.schema import DDL, SEED_ROWS, SEED_COLUMNS

DB = "stripboard"


def _ok(label: str) -> None:
    print(f"  \033[32mPASS\033[0m  {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  \033[31mFAIL\033[0m  {label}\n        {detail}")


def main() -> int:
    print("\nStripboard — stack verification (ClickHouse track)\n" + "-" * 52)
    failures = 0

    # 1. the official MCP server can reach the cluster at all
    try:
        client = create_clickhouse_client()
        _ok(f"mcp-clickhouse connected — server {client.server_version}")
    except Exception as exc:  # noqa: BLE001
        _fail("mcp-clickhouse could not connect", str(exc))
        print("\n  Is ClickHouse up?  docker compose up -d\n")
        return 1

    # 2. schema
    try:
        client.command(f"CREATE DATABASE IF NOT EXISTS {DB}")
        for stmt in DDL:
            client.command(stmt)
        _ok("schema applied")
    except Exception as exc:  # noqa: BLE001
        _fail("schema failed", str(exc))
        return 1

    # 3. a real MCP tool call
    try:
        dbs = json.loads(list_databases())
        assert DB in dbs, f"{DB} not in {dbs}"
        _ok(f"list_databases() MCP tool sees '{DB}'")
    except Exception as exc:  # noqa: BLE001
        _fail("list_databases() MCP tool", str(exc))
        failures += 1

    # 4. seed rows carrying a KNOWN continuity contradiction
    try:
        client.command(f"TRUNCATE TABLE {DB}.elements")
        client.insert(f"{DB}.elements", SEED_ROWS, column_names=SEED_COLUMNS)
        _ok(f"seeded {len(SEED_ROWS)} element rows")
    except Exception as exc:  # noqa: BLE001
        _fail("seed insert", str(exc))
        return 1

    # 5. analytical read back THROUGH the MCP tool
    try:
        res = json.loads(
            run_query(
                f"SELECT category, count() AS n FROM {DB}.elements "
                "GROUP BY category ORDER BY n DESC"
            )
        )
        assert res["rows"], "no rows"
        _ok(f"run_query() MCP tool returned {len(res['rows'])} category rows")
    except Exception as exc:  # noqa: BLE001
        _fail("run_query() MCP tool", str(exc))
        failures += 1

    # 6. the differentiator: does the store surface the contradiction we planted?
    try:
        res = json.loads(
            run_query(
                f"""
                SELECT category,
                       groupArray(concat('sc', scene_number, ' p',
                                  toString(page), ': ', element)) AS variants
                FROM {DB}.elements
                WHERE category = 'VEHICLE'
                GROUP BY category
                HAVING uniqExact(element) > 1
                """
            )
        )
        rows = res["rows"]
        assert rows, "planted contradiction was NOT surfaced"
        _ok("continuity contradiction surfaced via MCP:")
        for variant in rows[0][1]:
            print(f"          - {variant}")
    except Exception as exc:  # noqa: BLE001
        _fail("contradiction query", str(exc))
        failures += 1

    print("-" * 52)
    if failures:
        print(f"{failures} check(s) failed\n")
        return 1
    print("all checks passed — the required integration is live\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
