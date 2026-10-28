"""Data-quality suite: money dtypes, null keys, FX fallback-to-1 (exact, near-1 and flagged),
row-level FX consistency, currency sanity and row-count lineage."""
from __future__ import annotations

from decimal import Decimal
from statistics import median

import pyarrow as pa

from . import recon
from .control import control_round_usd
from .generate import ALLOWED_CCY, ENTITIES, REPORTING_CCY
from .pipeline import AMT, RATE, Lake

KEYS = ("txn_id", "entity", "account", "month", "currency")
MAX_MOM_MOVE = Decimal("0.10")      # >10% month-on-month rate move is suspicious
MAX_MEDIAN_DEV = Decimal("0.05")    # >5% away from the currency's median rate is suspicious
NEAR_ONE = Decimal("0.001")         # non-reporting rate within 0.1% of 1.0 is a likely fallback
# Currencies that legitimately trade at or near USD parity must be configured here; a near-1 rate for any
# other currency is treated as a suspected fallback (R4-03). Empty for the Contoso sample.
NEAR_PARITY_ALLOWLIST: frozenset[str] = frozenset()
MIN_HISTORY_MONTHS = 3          # below this, near-1 rates fail instead of being info-only (R3-04)
MONEY_TYPES = {"amount_local": AMT, "amount_usd": AMT, "fx_rate": RATE}


def money_dtypes(fact: pa.Table) -> dict:
    """F10: amounts must be DECIMAL, never float."""
    bad = {c: str(fact.schema.field(c).type) for c, t in MONEY_TYPES.items()
           if c not in fact.schema.names or fact.schema.field(c).type != t}
    return {"check": "money_dtypes", "passed": not bad, "bad_columns": bad}


def null_keys(fact) -> dict:
    cond = " OR ".join(f"{k} IS NULL OR trim({k}) = ''" for k in KEYS)
    n = recon._q(f"SELECT count(*) FROM f WHERE {cond}", f=fact)[0][0]
    return {"check": "null_keys", "passed": n == 0, "bad_rows": n}


def fx_fallback_to_one(fact, fx) -> dict:
    """FAIL on: non-reporting rows at exactly 1, rows flagged as fallback, rows with no FX rate.
    Near-1 rates are reported as information only (a currency can legitimately trade at parity, R2-10);
    a disguised fallback such as 1.0001 is caught by currency_sanity's outlier-vs-own-history check."""
    ones = recon._q("""SELECT currency, month, count(*) FROM f
        WHERE currency <> ? AND CAST(fx_rate AS DECIMAL(18,6)) = 1
        GROUP BY 1, 2 ORDER BY 1, 2""", [REPORTING_CCY], f=fact)
    near = recon._q("""SELECT currency, month, count(*) FROM f
        WHERE currency <> ? AND CAST(fx_rate AS DECIMAL(18,6)) <> 1
          AND abs(CAST(fx_rate AS DECIMAL(18,6)) - 1) <= CAST(? AS DECIMAL(18,6))
        GROUP BY 1, 2 ORDER BY 1, 2""", [REPORTING_CCY, str(NEAR_ONE)], f=fact)
    flagged = []
    if "fx_source" in fact.schema.names:
        flagged = recon._q("""SELECT currency, month, count(*) FROM f
            WHERE fx_source IS NULL OR fx_source <> 'bronze' GROUP BY 1, 2 ORDER BY 1, 2""", f=fact)
    missing = recon._q("""SELECT DISTINCT f.currency, f.month FROM f
        LEFT JOIN x ON x.currency = f.currency AND x.month = f.month
        WHERE x.rate_to_usd IS NULL ORDER BY 1, 2""", f=fact, x=fx)
    # R3-04: with too little history to judge "own level", a near-1 rate is a FAILURE, not information.
    hist = {c: n for c, n in recon._q("SELECT currency, count(DISTINCT month) FROM x GROUP BY 1", x=fx)}
    near_fail = [(c, m, n) for c, m, n in near
                 if hist.get(c, 0) < MIN_HISTORY_MONTHS and c not in NEAR_PARITY_ALLOWLIST]
    # R4-03: a currency within 0.1% of 1 in EVERY month of its history is a constant fallback, any history length
    stuck = [c for c, in recon._q("""SELECT currency FROM x WHERE currency <> ?
        GROUP BY 1 HAVING bool_and(abs(CAST(rate_to_usd AS DECIMAL(18,6)) - 1) <= CAST(? AS DECIMAL(18,6)))
        ORDER BY 1""", [REPORTING_CCY, str(NEAR_ONE)], x=fx) if c not in NEAR_PARITY_ALLOWLIST]
    return {"check": "fx_fallback_to_one",
            "passed": not ones and not missing and not flagged and not near_fail and not stuck,
            "constant_near_parity": stuck,
            "rate_one_groups": [{"currency": c, "month": m, "rows": n} for c, m, n in ones],
            "fallback_flagged": [{"currency": c, "month": m, "rows": n} for c, m, n in flagged],
            "near_one_short_history": [{"currency": c, "month": m, "rows": n} for c, m, n in near_fail],
            "near_one_info": [{"currency": c, "month": m, "rows": n} for c, m, n in near],
            "missing_rates": [{"currency": c, "month": m} for c, m in missing]}


def fx_row_consistency(fact, fx, limit: int = 20) -> dict:
    """F11: every row's fx_rate equals the bronze rate, and amount_usd = round_half_up(local x rate)."""
    rates = {(c, m): Decimal(str(r)) for c, m, r in recon._q("SELECT currency, month, rate_to_usd FROM x", x=fx)}
    bad, n = [], 0
    for row in fact.select(["txn_id", "currency", "month", "amount_local", "fx_rate", "amount_usd"]).to_pylist():
        try:
            loc, rate, usd = (Decimal(str(row[k])) for k in ("amount_local", "fx_rate", "amount_usd"))
            ok = usd == control_round_usd(loc, rate)
            src = rates.get((row["currency"], row["month"]))
            ok = ok and (src is None or src == rate)   # missing rates are reported by fx_fallback_to_one
        except Exception:  # noqa: BLE001 - null or unparsable money is itself a failure
            ok = False
        if not ok:
            n += 1
            if len(bad) < limit:
                bad.append(row["txn_id"])
    return {"check": "fx_row_consistency", "passed": n == 0, "bad_rows": n, "sample": bad}


def currency_sanity(fact, fx) -> dict:
    problems = []
    for c, in recon._q("SELECT DISTINCT currency FROM f", f=fact):
        if c not in ALLOWED_CCY:
            problems.append(f"unknown currency {c}")
    for e, c in recon._q("SELECT DISTINCT entity, currency FROM f", f=fact):
        if ENTITIES.get(e) != c:
            problems.append(f"entity {e} posted in {c}, functional currency is {ENTITIES.get(e)}")
    by_ccy: dict = {}
    for c, m, r in recon._q("SELECT currency, month, rate_to_usd FROM x ORDER BY 1, 2", x=fx):
        by_ccy.setdefault(c, []).append((m, Decimal(str(r))))
    for c, series in by_ccy.items():
        med = median(r for _, r in series)
        prev = None
        for m, r in series:
            if r <= 0:
                problems.append(f"non-positive rate {c} {m}")
            if c == REPORTING_CCY and r != 1:
                problems.append(f"reporting currency rate {r} != 1 for {m}")
            if prev and abs(r - prev) / prev > MAX_MOM_MOVE:
                problems.append(f"rate jump {c} {m}: {prev} -> {r}")
            if med > 0 and abs(r - med) / med > MAX_MEDIAN_DEV:
                problems.append(f"rate outlier {c} {m}: {r} vs median {med}")
            # R4-07: name near-parity months directly, so a poisoned median cannot shift the blame
            if c != REPORTING_CCY and c not in NEAR_PARITY_ALLOWLIST and abs(r - 1) <= NEAR_ONE:
                problems.append(f"near-parity rate {c} {m}: {r} (suspected fallback-to-1)")
            prev = r
    return {"check": "currency_sanity", "passed": not problems, "problems": problems}


def id_collisions(bronze) -> dict:
    """R2-05: ids that differ only by case/whitespace but carry DIFFERENT content are distinct transactions
    that normalisation would merge. A plain re-delivery (same content) is not a collision."""
    # R3-12: exact-same ids count too; R3-13: NULL-safe content key (a NULL field must not hide a difference)
    rows = recon._q("""SELECT upper(trim(txn_id)) AS k, list(DISTINCT txn_id) FROM b GROUP BY 1
        HAVING count(DISTINCT concat_ws('|', coalesce(entity, '<NULL>'), coalesce(account, '<NULL>'),
                                        coalesce(month, '<NULL>'), coalesce(currency, '<NULL>'),
                                        coalesce(CAST(amount_local AS VARCHAR), '<NULL>'))) > 1
        ORDER BY 1""", b=bronze)
    return {"check": "id_collision", "passed": not rows, "collisions": len(rows),
            "sample": [{"normalised_id": k, "variants": v} for k, v in rows[:20]]}


def lineage(lake: Lake) -> dict:
    """Row counts must be explainable: distinct normalised bronze txns = silver = gold = control."""
    b = recon.read(lake.bronze_txn)
    s, g, c = recon.read(lake.silver), recon.read(lake.gold), recon.read(lake.summary)
    distinct_b = recon._q("SELECT count(DISTINCT upper(trim(txn_id))) FROM b", b=b)[0][0]
    control = recon._q("SELECT sum(txn_count) FROM c", c=c)[0][0]
    counts = {"bronze": b.num_rows, "bronze_distinct_txn": distinct_b, "silver": s.num_rows,
              "gold": g.num_rows, "control": int(control)}
    ok = distinct_b == s.num_rows == g.num_rows == counts["control"]
    return {"check": "row_count_lineage", "passed": ok, "counts": counts}


def run_suite(lake: Lake) -> dict:
    gold, fx = recon.read(lake.gold), recon.read(lake.bronze_fx)
    checks = [money_dtypes(gold), null_keys(gold), fx_fallback_to_one(gold, fx),
              fx_row_consistency(gold, fx), currency_sanity(gold, fx),
              id_collisions(recon.read(lake.bronze_txn)), lineage(lake)]
    return {"passed": all(c["passed"] for c in checks), "checks": checks}
