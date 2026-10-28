"""Bronze -> silver -> gold medallion pipeline on local Delta tables (delta-rs), plus an
independently computed control summary.

The control summary is computed straight from the generator records in pure Python Decimal.
It never reads bronze/silver/gold, so it is an independent witness for reconciliation.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from .control import control_round_usd
from .generate import Dataset, to_reporting

AMT = pa.decimal128(18, 2)
RATE = pa.decimal128(18, 6)
SUM = pa.decimal128(38, 2)

RETENTION = {
    "delta.deletedFileRetentionDuration": "interval 7 days",
    "delta.logRetentionDuration": "interval 30 days",
}

FACT_SCHEMA = pa.schema([
    ("txn_id", pa.string()), ("entity", pa.string()), ("account", pa.string()),
    ("month", pa.string()), ("currency", pa.string()), ("amount_local", AMT),
    ("fx_rate", RATE), ("amount_usd", AMT), ("load_id", pa.int32()), ("fx_source", pa.string()),
])


class Lake:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path(self, name: str) -> str:
        return str(self.root / name)

    bronze_txn = property(lambda s: s.path("bronze/transactions"))
    bronze_fx = property(lambda s: s.path("bronze/fx"))
    silver = property(lambda s: s.path("silver/transactions"))
    gold = property(lambda s: s.path("gold/fact_gl"))
    summary = property(lambda s: s.path("control/summary"))


def _cols(rows: list[dict], schema: pa.Schema) -> pa.Table:
    return pa.Table.from_pydict({f.name: [r[f.name] for r in rows] for f in schema}, schema=schema)


# ---------------------------------------------------------------- bronze
BRONZE_SCHEMA = pa.schema([
    ("txn_id", pa.string()), ("entity", pa.string()), ("account", pa.string()),
    ("month", pa.string()), ("currency", pa.string()), ("amount_local", pa.string()),
    ("load_id", pa.int32()),
])
FX_SCHEMA = pa.schema([("currency", pa.string()), ("month", pa.string()), ("rate_to_usd", pa.string())])


def load_bronze(lake: Lake, ds: Dataset, redeliver: set[tuple[str, str]] = frozenset(),
                drop_fx: set[tuple[str, str]] = frozenset()) -> dict:
    """Land raw rows. `redeliver` = (entity, month) pairs the source sends twice (load_id 2),
    which is normal upstream behaviour that silver must de-duplicate."""
    rows = [{**t, "amount_local": str(t["amount_local"]), "load_id": 1} for t in ds.transactions]
    again = [{**r, "load_id": 2} for r in rows if (r["entity"], r["month"]) in redeliver]
    write_deltalake(lake.bronze_txn, _cols(rows + again, BRONZE_SCHEMA), mode="overwrite",
                    configuration=RETENTION)
    fx = [{**f, "rate_to_usd": str(f["rate_to_usd"])} for f in ds.fx
          if (f["currency"], f["month"]) not in drop_fx]
    write_deltalake(lake.bronze_fx, _cols(fx, FX_SCHEMA), mode="overwrite", configuration=RETENTION)
    return {"bronze_rows": len(rows) + len(again), "redelivered_rows": len(again), "fx_rows": len(fx)}


# ---------------------------------------------------------------- silver
def build_silver(lake: Lake, dedupe_bug: bool = False) -> dict:
    """Type, de-duplicate and apply FX.

    Correct de-dupe: one row per txn_id, latest load wins.
    Bug (incident A): de-dupe key wrongly includes load_id, so a re-delivered entity-month is doubled.
    FX: a missing rate silently falls back to 1.0 (the real-world bug class the DQ suite must catch).
    """
    raw = DeltaTable(lake.bronze_txn).to_pyarrow_table().to_pylist()
    fx = {(r["currency"], r["month"]): Decimal(r["rate_to_usd"])
          for r in DeltaTable(lake.bronze_fx).to_pyarrow_table().to_pylist()}
    keep: dict = {}
    for r in raw:
        r = {**r, "txn_id": normalise_id(r["txn_id"])}  # F07: case/whitespace variants are the same txn
        key = (r["txn_id"], r["load_id"]) if dedupe_bug else r["txn_id"]
        if key not in keep or r["load_id"] > keep[key]["load_id"]:
            keep[key] = r
    out = []
    for r in keep.values():
        found = (r["currency"], r["month"]) in fx
        rate = fx.get((r["currency"], r["month"]), Decimal("1"))  # legacy fallback, now flagged
        amt = Decimal(r["amount_local"])
        out.append({**r, "amount_local": amt, "fx_rate": rate, "amount_usd": to_reporting(amt, rate),
                    "fx_source": "bronze" if found else "fallback"})
    out.sort(key=lambda r: (r["month"], r["entity"], r["account"], r["txn_id"], r["load_id"]))
    write_deltalake(lake.silver, _cols(out, FACT_SCHEMA), mode="overwrite", configuration=RETENTION)
    return {"silver_rows": len(out)}


def normalise_id(txn_id):
    return txn_id.strip().upper() if isinstance(txn_id, str) else txn_id


# ---------------------------------------------------------------- gold
def build_gold(lake: Lake) -> dict:
    t = DeltaTable(lake.silver).to_pyarrow_table()
    write_deltalake(lake.gold, t, mode="overwrite", partition_by=["month"], configuration=RETENTION)
    return {"gold_rows": t.num_rows}


# ---------------------------------------------------------------- control summary
SUMMARY_SCHEMA = pa.schema([("entity", pa.string()), ("account", pa.string()), ("month", pa.string()),
                            ("txn_count", pa.int64()), ("amount_usd", SUM)])


def build_summary(lake: Lake, ds: Dataset) -> dict:
    """Independent control totals from the source records (not from any lake table)."""
    fx = {(f["currency"], f["month"]): f["rate_to_usd"] for f in ds.fx}
    agg: dict = defaultdict(lambda: [0, Decimal("0")])
    for t in ds.transactions:
        a = agg[(t["entity"], t["account"], t["month"])]
        a[0] += 1
        a[1] += control_round_usd(t["amount_local"], fx[(t["currency"], t["month"])])  # own rounding (F05)
    rows = [{"entity": e, "account": a, "month": m, "txn_count": c, "amount_usd": s}
            for (e, a, m), (c, s) in sorted(agg.items())]
    write_deltalake(lake.summary, _cols(rows, SUMMARY_SCHEMA), mode="overwrite", configuration=RETENTION)
    return {"summary_rows": len(rows)}


def run_pipeline(lake: Lake, ds: Dataset, redeliver=frozenset(), dedupe_bug=False, drop_fx=frozenset()) -> dict:
    stats = {}
    stats.update(load_bronze(lake, ds, set(redeliver), set(drop_fx)))
    stats.update(build_silver(lake, dedupe_bug=dedupe_bug))
    stats.update(build_gold(lake))
    stats.update(build_summary(lake, ds))
    return stats
