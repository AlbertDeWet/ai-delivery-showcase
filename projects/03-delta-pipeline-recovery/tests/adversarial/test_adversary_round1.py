"""ADVERSARY tests, round 1. Written by the adversarial agent, not the builder.

A test that FAILS proves a break. A test that PASSES proves the builder's defence held.
"""
import json
import re
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from deltalake import DeltaTable, write_deltalake

from deltarecover import dedupe, dq, pipeline, recon
from deltarecover.generate import CENT
from deltarecover.pipeline import FACT_SCHEMA, Lake, run_pipeline
from deltarecover.recover import _commit_ts, bad_load, find_pre_incident_version, recover, retention_window

M = "2025-02"
OTHER = "2025-03"
REDELIVER = {("CI-E02", "2025-01")}
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lake(tmp_path, small_ds):
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, small_ds, REDELIVER)
    return lk


def _gold(lake):
    return recon.read(lake.gold)


def _append(lake, t):
    write_deltalake(lake.gold, t.cast(FACT_SCHEMA), mode="append", partition_by=["month"])


def _rows(t, month):
    return t.filter(pc.equal(t["month"], month))


# ------------------------------------------------------------ 1. partial reload after the delete
def test_partial_reload_then_other_commit_picks_wrong_pre_incident_version(lake):
    """ADVERSARY: after delete -> partial reload of 2 rows -> unrelated commit, find_pre_incident_version
    picks the partial-reload version instead of the true pre-incident version 0."""
    full = _gold(lake)
    bad_load(lake, M)                                   # v1
    _append(lake, _rows(full, M).slice(0, 2))           # v2 partial reload
    _append(lake, _rows(full, OTHER).slice(0, 0))       # v3 unrelated (empty) commit
    write_deltalake(lake.gold, _rows(full, OTHER).slice(0, 1).cast(FACT_SCHEMA), mode="append",
                    partition_by=["month"])            # v4 unrelated commit
    assert find_pre_incident_version(DeltaTable(lake.gold), M) == 0


def test_partial_reload_recovery_does_not_complete_the_month(lake, tmp_path):
    """ADVERSARY: with a partial reload in place, recover cannot bring the month back to full
    (it should either recover all rows from v0 or say 'partial reload detected'; it just aborts)."""
    full = _gold(lake)
    bad_load(lake, M)
    _append(lake, _rows(full, M).slice(0, 2))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    assert ev["status"] == "RECOVERED" or "partial" in ev["status"].lower()


# ------------------------------------------------------------ 2. shared rounding
def test_shared_rounding_bug_is_invisible_to_reconciliation(tmp_path, small_ds, monkeypatch):
    """ADVERSARY: a rounding bug in to_reporting (truncate instead of half-up) is applied on BOTH sides
    of the reconciliation, so 0 breaks are reported although every converted amount may be off."""
    monkeypatch.setattr(pipeline, "to_reporting",
                        lambda a, r: (a * r).quantize(CENT, ROUND_DOWN))
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, small_ds)
    rec = recon.reconcile(recon.read(lk.gold), recon.read(lk.summary))
    # independent witness: half-up from source
    from decimal import ROUND_HALF_UP
    fx = {(f["currency"], f["month"]): f["rate_to_usd"] for f in small_ds.fx}
    truth = sum(((t["amount_local"] * fx[(t["currency"], t["month"])]).quantize(CENT, ROUND_HALF_UP)
                 for t in small_ds.transactions), Decimal("0"))
    assert Decimal(rec["gold_total_usd"]) != truth  # precondition: the bug moved money
    assert rec["break_count"] > 0, "reconciliation reported 0 breaks on wrongly rounded money"


# ------------------------------------------------------------ 3. float leakage
def _float_gold(lake):
    g = _gold(lake)
    i = g.schema.get_field_index("amount_usd")
    g = g.set_column(i, "amount_usd", pc.cast(g["amount_usd"], pa.float64()))
    write_deltalake(lake.gold, g, mode="overwrite", partition_by=["month"], schema_mode="overwrite")


def test_dq_suite_rejects_float_money_column(lake):
    """ADVERSARY: gold amount_usd silently rewritten as float64; the DQ suite still says PASS."""
    _float_gold(lake)
    assert dq.run_suite(lake)["passed"] is False


def test_recon_detects_float_money_column(lake):
    """ADVERSARY: reconciliation against a float64 amount column must not report 0 breaks."""
    _float_gold(lake)
    assert recon.reconcile(_gold(lake), recon.read(lake.summary))["break_count"] > 0


def test_row_level_amount_usd_matches_local_times_rate(lake):
    """ADVERSARY: fx_rate changed on a row while amount_usd is left alone; no check catches the
    inconsistency amount_usd != round(amount_local * fx_rate)."""
    g = _gold(lake)
    rows = g.to_pylist()
    for r in rows:
        if r["currency"] != "USD":
            r["fx_rate"] = r["fx_rate"] * 2
            break
    t = pa.Table.from_pylist(rows, schema=g.schema)
    write_deltalake(lake.gold, t, mode="overwrite", partition_by=["month"])
    assert dq.run_suite(lake)["passed"] is False


# ------------------------------------------------------------ 4. hours to vacuum <= 0
@pytest.mark.parametrize("offset_h", [0, -1])
def test_recover_refuses_at_or_past_horizon(lake, tmp_path, offset_h):
    """ADVERSARY: at exactly the vacuum horizon (0 h) or past it, recover must refuse and write nothing."""
    inc = bad_load(lake, M)
    dt = DeltaTable(lake.gold)
    now = _commit_ts(dt, inc["incident_version"]) + timedelta(days=7) - timedelta(hours=offset_h)
    v = dt.version()
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", now=now, write_evidence=False)
    assert ev["status"] == "ABORTED_RETENTION_EXPIRED" and DeltaTable(lake.gold).version() == v


def test_retention_window_ignores_log_retention(lake):
    """ADVERSARY: delta.logRetentionDuration of 1 hour makes the log (and time travel) disappear long
    before the 7-day file retention, but retention_window still reports the window as open for ~7 days."""
    inc = bad_load(lake, M)
    dt = DeltaTable(lake.gold)
    dt.alter.set_table_properties({"delta.logRetentionDuration": "interval 1 hours"})
    dt = DeltaTable(lake.gold)
    now = _commit_ts(dt, inc["incident_version"]) + timedelta(hours=5)
    w = retention_window(dt, inc["incident_version"], now)
    assert not w["open"] or w["hours_remaining"] <= 0


def test_unparseable_retention_aborts_cleanly(lake, tmp_path):
    """ADVERSARY: a retention of 'interval 1 weeks' (valid Delta syntax) crashes recover with ValueError
    instead of returning a refusal status."""
    bad_load(lake, M)
    DeltaTable(lake.gold).alter.set_table_properties({"delta.deletedFileRetentionDuration": "interval 1 weeks"})
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    except ValueError:
        pytest.fail("recover crashed on a valid Delta interval instead of refusing or parsing it")
    assert ev["status"] in ("RECOVERED", "ABORTED_RETENTION_EXPIRED")


# ------------------------------------------------------------ 5. duplicates the detector misses
@pytest.mark.parametrize("mangle", [str.lower, lambda s: " " + s, lambda s: s + " "])
def test_duplicate_detector_misses_case_and_whitespace(lake, mangle):
    """ADVERSARY: a duplicate row whose txn_id differs only by case or whitespace is not counted
    as a duplicate txn by dedupe.doubled_entity_months."""
    g = _gold(lake)
    row = g.slice(0, 1).to_pylist()[0]
    row["txn_id"] = mangle(row["txn_id"])
    _append(lake, pa.Table.from_pylist([row], schema=g.schema))
    r = dedupe.doubled_entity_months(_gold(lake), recon.read(lake.summary))
    assert r["duplicate_txn_ids"] >= 1


def test_silver_dedupe_misses_case_variant_redelivery(tmp_path, small_ds):
    """ADVERSARY: the source re-delivers a txn with a lower-cased id under load_id 2; silver keeps both."""
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, small_ds)
    b = recon.read(lk.bronze_txn)
    row = b.slice(0, 1).to_pylist()[0]
    row.update(txn_id=row["txn_id"].lower(), load_id=2)
    write_deltalake(lk.bronze_txn, pa.Table.from_pylist([row], schema=b.schema), mode="append")
    pipeline.build_silver(lk)
    assert recon.read(lk.silver).num_rows == len(small_ds.transactions)


def test_same_key_different_load_id_in_gold_is_caught(lake):
    """ADVERSARY: same txn_id with a different load_id appended to gold must be counted (defence check)."""
    g = _gold(lake)
    row = g.slice(0, 1).to_pylist()[0]
    row["load_id"] = 9
    _append(lake, pa.Table.from_pylist([row], schema=g.schema))
    assert dedupe.doubled_entity_months(_gold(lake), recon.read(lake.summary))["duplicate_txn_ids"] == 1


# ------------------------------------------------------------ 6. FX
def _fx_override(tmp_path, small_ds, ccy, month, rate):
    lk = Lake(tmp_path / "lake")
    pipeline.load_bronze(lk, small_ds)
    fx = recon.read(lk.bronze_fx).to_pylist()
    for r in fx:
        if r["currency"] == ccy and r["month"] == month:
            r["rate_to_usd"] = rate
    write_deltalake(lk.bronze_fx, pa.Table.from_pylist(fx, schema=pipeline.FX_SCHEMA), mode="overwrite")
    pipeline.build_silver(lk)
    pipeline.build_gold(lk)
    pipeline.build_summary(lk, small_ds)
    return lk


@pytest.mark.parametrize("month", ["2025-01", "2025-02"])
def test_fx_fallback_disguised_as_1_0001(tmp_path, small_ds, month):
    """ADVERSARY: a EUR rate of 1.0001 (fallback disguised as 'not exactly 1') passes the DQ suite:
    the =1 check misses it and a 1.08 -> 1.0001 move is under the 10% jump threshold."""
    lk = _fx_override(tmp_path, small_ds, "EUR", month, "1.0001")
    fxc = dq.fx_fallback_to_one(_gold(lk), recon.read(lk.bronze_fx))
    san = dq.currency_sanity(_gold(lk), recon.read(lk.bronze_fx))
    assert not (fxc["passed"] and san["passed"]), "near-1 EUR rate went undetected"


def test_fx_rate_exactly_one_with_scale_is_caught(tmp_path, small_ds):
    """ADVERSARY: '1.000000' (same value, different text) for EUR must still be caught (defence check)."""
    lk = _fx_override(tmp_path, small_ds, "EUR", "2025-02", "1.000000")
    assert not dq.fx_fallback_to_one(_gold(lk), recon.read(lk.bronze_fx))["passed"]


def test_missing_usd_rate_is_reported_but_amounts_correct(tmp_path, small_ds):
    """ADVERSARY: a missing rate for USD (legitimately 1) gives correct amounts but must still be reported
    as a missing rate, not silently accepted."""
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, small_ds, drop_fx={("USD", "2025-02")})
    assert recon.reconcile(_gold(lk), recon.read(lk.summary))["break_count"] == 0
    assert {"currency": "USD", "month": "2025-02"} in dq.fx_fallback_to_one(
        _gold(lk), recon.read(lk.bronze_fx))["missing_rates"]


# ------------------------------------------------------------ 7. crash between restore and verify
def test_crash_between_restore_and_verify_leaves_table_unverified(lake, tmp_path):
    """ADVERSARY: an exception after the append but before live verify propagates out of recover:
    no rollback, no evidence, and the table is left at an unverified version."""
    bad_load(lake, M)
    v_before = DeltaTable(lake.gold).version()

    def crash(_):
        raise RuntimeError("process killed")
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk", inject_fault=crash)
    ev_path = tmp_path / "res" / "recovery_evidence.json"
    assert DeltaTable(lake.gold).version() == v_before or ev_path.exists(), \
        "crash left an unverified write with no rollback and no evidence"


def test_rerun_after_crash_cannot_verify(lake, tmp_path):
    """ADVERSARY: after a crash mid-recovery, re-running recover cannot verify the already-written rows."""
    bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk",
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
    ev = recover(lake, M, tmp_path / "res2", tmp_path / "bk2", write_evidence=False)
    assert ev["status"] in ("RECOVERED", "ALREADY_RECOVERED")


def test_rollback_destroys_concurrent_writer_commit(lake, tmp_path):
    """ADVERSARY: a legitimate concurrent commit lands between the restore append and verify; the
    rollback RESTOREs to the pre-restore version and silently discards the other writer's data."""
    full = _gold(lake)
    bad_load(lake, M)
    extra = _rows(full, OTHER).slice(0, 1).to_pylist()[0]
    extra.update(txn_id="CONCURRENT-1")

    def concurrent(lk):
        _append(lk, pa.Table.from_pylist([extra], schema=full.schema))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", inject_fault=concurrent, write_evidence=False)
    ids = set(_gold(lake)["txn_id"].to_pylist())
    assert "CONCURRENT-1" in ids or ev["status"] not in ("ROLLED_BACK",), \
        "rollback silently discarded a concurrent writer's commit"


# ------------------------------------------------------------ 8. backup
def test_backup_is_a_readable_delta_table_with_pre_incident_history(lake, tmp_path):
    """ADVERSARY: the backup must be restorable on its own: readable as Delta, same rows as live at backup
    time, and time travel to the pre-incident version still returns the month (defence check)."""
    bad_load(lake, M)
    rows_at_backup = _gold(lake).num_rows
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    bp = ev["backup"]["backup_path"]
    assert DeltaTable(bp).to_pyarrow_table().num_rows == rows_at_backup
    assert _rows(DeltaTable(bp, version=0).to_pyarrow_table(), M).num_rows > 0


def test_tampered_backup_not_detected_in_live_verify(lake, tmp_path):
    """ADVERSARY: the backup is corrupted after the dry run; live V12 never re-hashes it, so recover
    reports RECOVERED with an unusable backup."""
    bad_load(lake, M)

    def tamper(lk):
        bk = next(p for p in (tmp_path / "bk").iterdir() if p.is_dir())
        f = next(bk.rglob("*.parquet"))
        f.write_bytes(b"corrupt")
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", inject_fault=tamper, write_evidence=False)
    v12 = next(t for t in ev["live_verify"] if t["id"] == "V12")
    assert v12["passed"] is False


def test_dry_run_v12_does_not_compare_live_table_to_backup(lake, tmp_path, monkeypatch):
    """ADVERSARY: the code comment says the dry run checks 'the live table must still equal the backup',
    but V12 only hashes the backup against its own manifest. A write to live after backup is not caught by V12."""
    from deltarecover import recover as R
    bad_load(lake, M)
    real = R.backup
    full = _gold(lake)

    def backup_then_write(lk, dest):
        b = real(lk, dest)
        row = _rows(full, OTHER).slice(0, 1).to_pylist()[0]
        row["txn_id"] = "LATE-1"
        _append(lk, pa.Table.from_pylist([row], schema=full.schema))
        return b
    monkeypatch.setattr(R, "backup", backup_then_write)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    v12 = next(t for t in ev["dry_run"] if t["id"] == "V12")
    assert v12["passed"] is False


# ------------------------------------------------------------ 9. SQL injection via month
INJ = "2025-01' OR '1'='1"


def test_bad_load_month_injection_deletes_everything(lake):
    """ADVERSARY: month is f-stringed into the Delta delete predicate; an injected month deletes every row."""
    before = _gold(lake).num_rows
    try:
        bad_load(lake, INJ)
    except (ValueError, Exception):
        pass
    assert _gold(lake).num_rows == before


def test_recover_rejects_malformed_month(lake, tmp_path):
    """ADVERSARY: recover must validate month as YYYY-MM before any SQL; it does not."""
    bad_load(lake, M)
    with pytest.raises(ValueError):
        recover(lake, "2025-02' OR '1'='1", tmp_path / "res", tmp_path / "bk", write_evidence=False)


def test_recon_cells_month_injection(lake):
    """ADVERSARY: recon.cells(month=...) is injectable: an injected month returns every cell of the table."""
    g = _gold(lake)
    assert recon.cells(g, INJ) == {}


# ------------------------------------------------------------ 10. README vs results
def _readme_table():
    txt = (ROOT / "README.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| ([^|]+?) \| ([^|]+?) \|$", txt, re.M))


def test_readme_numbers_match_results_json():
    """ADVERSARY: every number in the README results table must come from results/demo_results.json."""
    r = json.loads((ROOT / "results" / "demo_results.json").read_text())
    t = _readme_table()
    rec = r["recovery"]
    checks = {
        "Source transactions": f"{r['dataset']['transactions']:,}",
        "Rows deleted by the bad load": f"{r['incident_b']['rows_deleted']:,}",
        "Dry-run verify tests passed": f"{rec['dry_run_passed']} / {rec['verify_tests']}",
        "Entity x account cells reconciled to the cent / breaks": f"{rec['cells']} / {rec['breaks']}",
        "Backup start -> live verify passing": f"{round(rec['first_verify_pass_s'], 1)} s",
    }
    for k, v in checks.items():
        row = next(val for key, val in t.items() if key.startswith(k))
        assert row == v, (k, row, v)
    assert t["Doubled entity-months before / after the de-dupe fix (of 96)"] == \
        f"{r['incident_a']['before']['doubled_count']} / {r['incident_a']['after']['doubled_count']}"
    assert any(str(rec["hours_remaining"]) == v for k, v in t.items() if k.startswith("Hours left"))


def test_readme_builder_test_count_is_recorded_in_results():
    """ADVERSARY: README claims 'Builder tests | 19 passed' but no file in results/ records a test run."""
    t = _readme_table()
    claim = t["Builder tests"]
    n = re.search(r"\d+", claim).group(0)
    blob = "".join(p.read_text(errors="ignore") for p in (ROOT / "results").rglob("*") if p.is_file())
    assert re.search(rf"{n} passed", blob), f"'{claim}' has no recorded run in results/"


def test_evidence_pack_matches_demo_results():
    """ADVERSARY: results/recovery_evidence.json (the evidence pack) must agree with demo_results.json."""
    r = json.loads((ROOT / "results" / "demo_results.json").read_text())
    ev = json.loads((ROOT / "results" / "recovery_evidence.json").read_text())
    assert ev["status"] == r["recovery"]["status"]
    assert ev["restored_rows"] == r["recovery"]["restored_rows"]
    assert ev["reconciliation"]["gold_total_usd"] == r["recovery"]["gold_total_usd"]
