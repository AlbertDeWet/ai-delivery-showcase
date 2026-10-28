"""ADVERSARY tests, round 3. Written by the adversarial agent, not the builder.

A FAILING test proves an open break. A PASSING test proves the builder's defence held.
Kept fast: a tiny dataset (4 accounts x 3 months) for every recover() call.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from deltalake import CommitProperties, DeltaTable, write_deltalake

from deltarecover import dq, pipeline, recon
from deltarecover import recover as R
from deltarecover.generate import generate
from deltarecover.pipeline import Lake, run_pipeline
from deltarecover.recover import BATCH_KEY, Journal, bad_load, recover

M = "2025-02"
OTHER = "2025-03"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tiny_ds():
    return generate(seed=11, n_accounts=4, n_months=3, max_txn=3)


@pytest.fixture
def lake(tmp_path, tiny_ds):
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, tiny_ds)
    return lk


def _gold(lk):
    return recon.read(lk.gold)


def _m(t, month):
    return t.filter(pc.equal(t["month"], month))


def _append(lk, t, meta=None):
    kw = {"commit_properties": CommitProperties(custom_metadata=meta)} if meta else {}
    write_deltalake(lk.gold, t.cast(_gold(lk).schema), mode="append", partition_by=["month"], **kw)


def _ev(tmp_path):
    p = tmp_path / "res" / "recovery_evidence.json"
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------------ A. incident_version misuse
def test_future_incident_version_refuses_with_evidence(lake, tmp_path):
    """ADVERSARY: incident_version beyond the table's history raises a bare LookupError out of recover
    (only AmbiguousIncident is caught), so there is no refusal status and no evidence."""
    inc = bad_load(lake, M)
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"] + 50)
    except LookupError:
        ev = None
    assert ev is not None and ev["status"].startswith("ABORTED") and _ev(tmp_path) is not None


def test_wrong_incident_version_is_validated(lake, tmp_path):
    """ADVERSARY: an operator-supplied incident_version that did NOT empty the month (an unrelated later commit)
    is accepted without validation; recover then fails with a generic dry-run status instead of naming the bad input."""
    full = _gold(lake)
    inc = bad_load(lake, M)
    _append(lake, _m(full, OTHER).slice(0, 0))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"] + 1,
                 write_evidence=False)
    assert ev["status"] not in ("RECOVERED", "ALREADY_RECOVERED")
    assert "INCIDENT" in ev["status"], ev["status"]


def test_wrong_incident_version_never_writes(lake, tmp_path):
    """ADVERSARY: whatever the wrong version, nothing may be written to gold (defence check)."""
    inc = bad_load(lake, M)
    _append(lake, _m(_gold(lake), OTHER).slice(0, 0))
    v = DeltaTable(lake.gold).version()
    recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"] + 1,
            write_evidence=False)
    assert DeltaTable(lake.gold).version() == v


def test_vacuumed_pre_incident_files_refuse_with_evidence(lake, tmp_path):
    """ADVERSARY: someone VACUUMs with the retention check disabled. The property-based window still says
    'open', and reading the pre-incident version crashes before V02 can report 'files missing'."""
    inc = bad_load(lake, M)
    DeltaTable(lake.gold).vacuum(retention_hours=0, enforce_retention_duration=False, dry_run=False)
    v = DeltaTable(lake.gold).version()
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    except Exception:  # noqa: BLE001
        ev = None
    assert ev is not None and ev["status"].startswith("ABORTED"), "crash instead of a refusal"
    assert DeltaTable(lake.gold).version() == v


# ------------------------------------------------------------ B. batch-id metadata
def test_forged_batch_metadata_makes_resume_discard_foreign_rows(lake, tmp_path):
    """ADVERSARY: any writer can put deltarecover.batch_id in commit metadata. A foreign commit tagged that way,
    carrying one bad row of the month AND one legitimate row of another month, is rolled back by resume clean-up:
    the legitimate foreign row is silently discarded."""
    full = _gold(lake)
    inc = bad_load(lake, M)
    bad = _m(full, M).slice(0, 1).to_pylist()[0]
    bad["amount_usd"] += Decimal("5.00")
    legit = _m(full, OTHER).slice(0, 1).to_pylist()[0]
    legit["txn_id"] = "FOREIGN-LEGIT-1"
    _append(lake, pa.Table.from_pylist([bad, legit], schema=full.schema), meta={BATCH_KEY: "forged"})
    recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
            write_evidence=False)
    assert "FOREIGN-LEGIT-1" in set(_gold(lake)["txn_id"].to_pylist())


def test_resume_cleanup_rollback_happens_after_backup(lake, tmp_path, monkeypatch):
    """ADVERSARY: the resume clean-up RESTOREs the table BEFORE the backup step runs, so the method's
    'backup before every change' rule is broken on the resume path."""
    inc = bad_load(lake, M)
    order = []
    real_b, real_r = R.backup, R._rollback_to
    monkeypatch.setattr(R, "backup", lambda *a, **k: (order.append("backup"), real_b(*a, **k))[1])
    monkeypatch.setattr(R, "_rollback_to", lambda *a, **k: (order.append("rollback"), real_r(*a, **k))[1])
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                corrupt_restore=lambda b: pa.concat_tables([b, b]),
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    order.clear()
    recover(lake, M, tmp_path / "res2", tmp_path / "bk2", incident_version=inc["incident_version"],
            write_evidence=False)
    assert "rollback" in order and order.index("backup") < order.index("rollback"), order


def test_resume_after_corrupt_crash_recovers_exactly(lake, tmp_path):
    """ADVERSARY: crash after a doubled batch, then resume: the month ends with exactly the pre-incident rows (defence check)."""
    full = _gold(lake)
    inc = bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                corrupt_restore=lambda b: pa.concat_tables([b, b]),
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    ev = recover(lake, M, tmp_path / "res2", tmp_path / "bk2", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"] == "RECOVERED"
    assert sorted(_m(_gold(lake), M)["txn_id"].to_pylist()) == sorted(_m(full, M)["txn_id"].to_pylist())


def test_untagged_inconsistent_batch_gets_specific_refusal(lake, tmp_path):
    """ADVERSARY: a doubled restore written WITHOUT batch metadata (an older tool, or metadata stripped) leaves
    the month inconsistent; recover refuses (safe) but only with the generic ABORTED_DRY_RUN_FAILED."""
    full = _gold(lake)
    inc = bad_load(lake, M)
    _append(lake, pa.concat_tables([_m(full, M), _m(full, M)]))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"].startswith("ABORTED") and ev["status"] != "ABORTED_DRY_RUN_FAILED", ev["status"]


def test_resume_after_partial_consistent_commit_tops_up(lake, tmp_path):
    """ADVERSARY: a tagged batch that committed only part of the month (consistent rows) is kept and topped up (defence check)."""
    full = _gold(lake)
    inc = bad_load(lake, M)
    _append(lake, _m(full, M).slice(0, 2), meta={BATCH_KEY: "partial"})
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"] == "RECOVERED" and _m(_gold(lake), M).num_rows == _m(full, M).num_rows


# ------------------------------------------------------------ C. append-only journal
def test_concurrent_runs_lose_journal_entries(tmp_path):
    """ADVERSARY: two runs sharing a results dir each hold their own in-memory history and rewrite the whole file;
    the later writer erases the other run's entries."""
    ja = Journal(tmp_path, {"run_id": "A"})
    jb = Journal(tmp_path, {"run_id": "B"})
    ja.step("1_retention_check")
    jb.step("1_retention_check")
    ja.step("2_backup")
    runs = {e["run_id"] for e in json.loads((tmp_path / "recovery_journal.json").read_text())}
    assert runs == {"A", "B"}, runs


def test_huge_journal_step_cost(tmp_path):
    """ADVERSARY: the 'append-only' journal re-reads and rewrites the entire JSON file on every step (O(n) per step,
    O(n^2) per history). With 150k historic entries, one step must still be cheap (< 0.5 s)."""
    hist = [{"run_id": "old", "step": f"s{i}", "at_utc": "2026-01-01T00:00:00+00:00"} for i in range(150_000)]
    (tmp_path / "recovery_journal.json").write_text(json.dumps(hist))
    j = Journal(tmp_path, {"run_id": "new"})
    t = time.perf_counter()
    j.step("x")
    assert time.perf_counter() - t < 0.5


def test_unreadable_journal_is_moved_aside_not_lost(tmp_path):
    """ADVERSARY: a corrupt journal is preserved aside and a new one started (defence check)."""
    (tmp_path / "recovery_journal.json").write_text("{not json")
    Journal(tmp_path, {"run_id": "n"}).step("a")
    assert list(tmp_path.glob("recovery_journal.unreadable-*.json"))
    assert json.loads((tmp_path / "recovery_journal.json").read_text())[0]["run_id"] == "n"


# ------------------------------------------------------------ D. id_collision
def _bronze(rows):
    return pa.Table.from_pylist(rows, schema=pipeline.BRONZE_SCHEMA)


BASE = {"txn_id": "T1", "entity": "CI-E01", "account": "4000", "month": M, "currency": "USD",
        "amount_local": "10.00", "load_id": 1}


def test_id_collision_misses_exact_same_id_different_content():
    """ADVERSARY: two different transactions with the EXACT same txn_id (different entity and amount) are not
    reported, because the check needs more than one distinct raw id; silver keeps only one."""
    other = dict(BASE, entity="CI-E02", currency="EUR", amount_local="99.00")
    assert not dq.id_collisions(_bronze([BASE, other]))["passed"]


def test_id_collision_flags_case_variant_with_different_content():
    """ADVERSARY: 'T1' vs 't1' with different amounts is flagged (defence check)."""
    assert not dq.id_collisions(_bronze([BASE, dict(BASE, txn_id="t1", amount_local="11.00")]))["passed"]


def test_id_collision_null_account_hides_collision():
    """ADVERSARY: the content key is built with ||, so a NULL account makes it NULL, and count(DISTINCT) ignores it;
    'T1' vs ' t1' with a NULL account and a different amount is not flagged."""
    a = dict(BASE, account=None)
    b = dict(BASE, txn_id=" t1", account=None, amount_local="77.00")
    assert not dq.id_collisions(_bronze([a, b]))["passed"]


def test_id_collision_redelivery_is_not_a_false_positive():
    """ADVERSARY: a plain re-delivery with a case variant and identical content is not a collision (defence check)."""
    assert dq.id_collisions(_bronze([BASE, dict(BASE, txn_id=" t1", load_id=2)]))["passed"]


# ------------------------------------------------------------ E. FX outlier logic on short histories
def _fx_lake(tmp_path, n_months, overrides):
    ds = generate(seed=5, n_accounts=2, n_months=n_months, max_txn=1)
    lk = Lake(tmp_path / f"fx{n_months}")
    pipeline.load_bronze(lk, ds)
    fx = recon.read(lk.bronze_fx).to_pylist()
    for r in fx:
        if (r["currency"], r["month"]) in overrides:
            r["rate_to_usd"] = overrides[(r["currency"], r["month"])]
    write_deltalake(lk.bronze_fx, pa.Table.from_pylist(fx, schema=pipeline.FX_SCHEMA), mode="overwrite")
    pipeline.build_silver(lk)
    pipeline.build_gold(lk)
    return lk


@pytest.mark.parametrize("n_months,month", [(1, "2025-01"), (2, "2025-02")])
def test_disguised_fallback_on_short_history(tmp_path, n_months, month):
    """ADVERSARY: with 1 or 2 months of history a EUR rate of 1.0001 passes DQ: near-1 is info-only since R2-10,
    there is no previous month (1) or the move is < 10% (2), and the median of <= 2 points absorbs it."""
    lk = _fx_lake(tmp_path, n_months, {("EUR", month): "1.000100"})
    g, fx = _gold(lk), recon.read(lk.bronze_fx)
    passed = dq.fx_fallback_to_one(g, fx)["passed"] and dq.currency_sanity(g, fx)["passed"]
    assert not passed, "disguised fallback undetected on a short history"


def test_two_month_legit_move_is_not_flagged(tmp_path):
    """ADVERSARY: a legitimate 8% move across a 2-month history must not be a false positive (defence check)."""
    lk = _fx_lake(tmp_path, 2, {("GBP", "2025-01"): "1.200000", ("GBP", "2025-02"): "1.296000"})
    assert dq.currency_sanity(_gold(lk), recon.read(lk.bronze_fx))["passed"]


# ------------------------------------------------------------ F. shared state / xdist / threads
def test_shared_duckdb_connection_is_thread_safe():
    """ADVERSARY: recon._q uses ONE global DuckDB connection and registers tables under fixed names ('f', 's').
    Two threads reconciling different tables can read each other's registration or error out."""
    a = pa.table({"entity": ["A"] * 50, "account": ["1"] * 50, "month": [M] * 50,
                  "amount_usd": pa.array([Decimal("1.00")] * 50, pa.decimal128(18, 2))})
    b = pa.table({"entity": ["B"] * 7, "account": ["2"] * 7, "month": [M] * 7,
                  "amount_usd": pa.array([Decimal("2.00")] * 7, pa.decimal128(18, 2))})
    exp_a, exp_b = recon.cells(a), recon.cells(b)

    def work(i):
        t, e = (a, exp_a) if i % 2 else (b, exp_b)
        try:
            return recon.cells(t) == e
        except Exception:  # noqa: BLE001
            return False
    with ThreadPoolExecutor(8) as ex:
        ok = list(ex.map(work, range(400)))
    assert all(ok), f"{ok.count(False)} of 400 concurrent calls wrong or failed"


def test_no_test_writes_into_the_repo():
    """ADVERSARY: under xdist, tests must not write shared paths in the repo (results/, work/). Every recover call
    in the test suites must pass tmp_path-based dirs (static scan; defence check)."""
    offenders = []
    for f in (ROOT / "tests").rglob("test_*.py"):
        if f.name == Path(__file__).name:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"recover\(", line) and re.search(r"ROOT|\"results\"|'results'|\"work\"", line):
                offenders.append(f"{f.name}:{n}")
            if re.search(r"(write_text|open\(.*['\"]w)", line) and "ROOT" in line:
                offenders.append(f"{f.name}:{n}")
    assert not offenders, offenders
