"""Execute guarded SQL on DuckDB, read-only and with external access disabled."""

from __future__ import annotations

import threading
from pathlib import Path

import duckdb

from nl2sql import config


def connect(path: Path | str = config.DB_PATH) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=True, config={
        "enable_external_access": False,
        "memory_limit": config.MEMORY_LIMIT,
        "threads": config.THREADS,
    })


def run(sql: str, con: duckdb.DuckDBPyConnection, timeout_s: float = config.QUERY_TIMEOUT_S):
    """Return (columns, rows). Interrupts the query if it exceeds timeout_s."""
    timer = threading.Timer(timeout_s, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        timer.cancel()
    return cols, rows
