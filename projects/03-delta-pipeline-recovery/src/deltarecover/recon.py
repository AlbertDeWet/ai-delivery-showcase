"""Reconciliation helpers. DuckDB sums DECIMAL exactly; results come back as Python Decimal.
No float ever touches an amount. Every caller-supplied value is a bound parameter (round 1 fix F01)."""
from __future__ import annotations

import re
import threading
from decimal import Decimal

import duckdb
import pyarrow as pa
from deltalake import DeltaTable

MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")  # ASCII digits only (R2-01)


def validate_month(month: str) -> str:
    if not isinstance(month, str) or not MONTH_RE.fullmatch(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    return month


def read(path: str, version: int | None = None) -> pa.Table:
    dt = DeltaTable(path, version=version) if version is not None else DeltaTable(path)
    return dt.to_pyarrow_table()


_LOCAL = threading.local()


def _con() -> duckdb.DuckDBPyConnection:
    """One in-memory DuckDB connection PER THREAD (R3-03): connect is slow on Windows, so it is reused,
    but table registrations ('f', 's', ...) are connection-scoped and must never be shared across threads."""
    con = getattr(_LOCAL, "con", None)
    if con is None:
        con = _LOCAL.con = duckdb.connect()
    return con


def _q(sql: str, params: list | None = None, **tables) -> list[tuple]:
    con = _con()
    for name, t in tables.items():
        con.register(name, t)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        for name in tables:
            con.unregister(name)


def _month_filter(month: str | None) -> tuple[str, list]:
    return ("WHERE month = ?", [month]) if month is not None else ("", [])


def cells(fact: pa.Table, month: str | None = None) -> dict[tuple[str, str], tuple[int, Decimal]]:
    where, p = _month_filter(month)
    rows = _q(f"SELECT entity, account, count(*), sum(amount_usd) FROM f {where} GROUP BY 1, 2", p, f=fact)
    return {(e, a): (n, Decimal(str(s))) for e, a, n, s in rows}


def summary_cells(summary: pa.Table, month: str | None = None) -> dict[tuple[str, str], tuple[int, Decimal]]:
    where, p = _month_filter(month)
    rows = _q(f"SELECT entity, account, sum(txn_count), sum(amount_usd) FROM s {where} GROUP BY 1, 2", p,
              s=summary)
    return {(e, a): (int(n), Decimal(str(v))) for e, a, n, v in rows}


def entity_month(fact: pa.Table) -> dict[tuple[str, str], tuple[int, Decimal]]:
    rows = _q("SELECT entity, month, count(*), sum(amount_usd) FROM f GROUP BY 1, 2", f=fact)
    return {(e, m): (n, Decimal(str(s))) for e, m, n, s in rows}


def summary_entity_month(summary: pa.Table) -> dict[tuple[str, str], tuple[int, Decimal]]:
    rows = _q("SELECT entity, month, sum(txn_count), sum(amount_usd) FROM s GROUP BY 1, 2", s=summary)
    return {(e, m): (int(n), Decimal(str(v))) for e, m, n, v in rows}


def reconcile(fact: pa.Table, summary: pa.Table, month: str | None = None) -> dict:
    """Compare every entity x account cell to the cent. A cell present on only one side is a break."""
    g, s = cells(fact, month), summary_cells(summary, month)
    breaks = []
    for key in sorted(set(g) | set(s)):
        gn, ga = g.get(key, (0, Decimal("0.00")))
        sn, sa = s.get(key, (0, Decimal("0.00")))
        if gn != sn or ga != sa:
            breaks.append({"entity": key[0], "account": key[1], "gold_rows": gn, "summary_rows": sn,
                           "gold_usd": str(ga), "summary_usd": str(sa), "diff_usd": str(ga - sa)})
    total_g = sum((v[1] for v in g.values()), Decimal("0"))
    total_s = sum((v[1] for v in s.values()), Decimal("0"))
    return {"cells": len(set(g) | set(s)), "breaks": breaks, "break_count": len(breaks),
            "gold_total_usd": str(total_g), "summary_total_usd": str(total_s),
            "diff_total_usd": str(total_g - total_s)}
