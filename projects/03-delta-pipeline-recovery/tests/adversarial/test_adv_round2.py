"""ADVERSARY tests, round 2. Written by the adversarial agent, not the builder.

A FAILING test proves an open break. A PASSING test proves the builder's defence held.
"""
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from deltalake import DeltaTable, write_deltalake

from deltarecover import dq, pipeline, recon
from deltarecover import recover as R
from deltarecover.pipeline import Lake, run_pipeline
from deltarecover.recover import bad_load, find_pre_incident_version, recover

M = "2025-02"
OTHER = "2025-03"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lake(tmp_path, small_ds):
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, small_ds)
    return lk


def _gold(lk):
    return recon.read(lk.gold)


def _m(t, month):
    return t.filter(pc.equal(t["month"], month))


def _append(lk, t):
    write_deltalake(lk.gold, t.cast(_gold(lk).schema), mode="append", partition_by=["month"])


def _month_ids(lk, month):
    return sorted(_m(_gold(lk), month)["txn_id"].to_pylist())


# ------------------------------------------------------------ A. string-built delete predicate
@pytest.mark.parametrize("bad", ["2025-02\n", "٢٠٢٥-02", "2025-02 ", "2025-13"])
def test_validate_month_is_strict_ascii(bad):
    """ADVERSARY: validate_month must accept only ASCII YYYY-MM. `\\d` also matches Unicode digits
    (Arabic-Indic '٢٠٢٥-02' passes), so the 'strict' validation is not strict."""
    with pytest.raises(ValueError):
        recon.validate_month(bad)


def test_bad_load_injection_still_blocked(lake):
    """ADVERSARY: the round-1 injection and a quote-escape variant are rejected and delete nothing (defence check)."""
    n = _gold(lake).num_rows
    for bad in ("2025-02' OR '1'='1", "2025-02'' OR ''1''=''1", "2025-02'--"):
        with pytest.raises(ValueError):
            bad_load(lake, bad)
    assert _gold(lake).num_rows == n


# ------------------------------------------------------------ B. version selection from commit history
def test_later_legit_delete_of_same_month_moves_pre_incident_version(lake):
    """ADVERSARY: after the bad load, a recovery reload and then a LEGITIMATE narrow delete of one entity
    in the same month (predicate still names the month). History selection picks the legit delete,
    not the bad load, so the 'pre-incident' version is the post-reload one."""
    full = _gold(lake)
    bad_load(lake, M)                                           # v1 the incident
    _append(lake, _m(full, M))                                  # v2 someone reloads it
    DeltaTable(lake.gold).delete(f"month = '{M}' AND entity = 'CI-E01'")  # v3 legit narrow delete
    assert find_pre_incident_version(DeltaTable(lake.gold), M) == 0


def test_overwrite_bad_load_with_partial_reload_picks_wrong_version(lake):
    """ADVERSARY: bad load done as an OVERWRITE without the month (no DELETE commit), then a 2-row partial
    reload and an unrelated commit. The admitted row-presence fallback picks the partial version."""
    full = _gold(lake)
    write_deltalake(lake.gold, full.filter(pc.not_equal(full["month"], M)), mode="overwrite",
                    partition_by=["month"])                     # v1 incident (overwrite)
    _append(lake, _m(full, M).slice(0, 2))                      # v2 partial reload
    _append(lake, _m(full, OTHER).slice(0, 0))                  # v3 unrelated
    assert find_pre_incident_version(DeltaTable(lake.gold), M) == 0


def test_overwrite_bad_load_recovers_completely(lake, tmp_path):
    """ADVERSARY: after an overwrite-style bad load with a partial reload, recover must bring the month
    back to the full pre-incident rows (or refuse); it must never report success with a partial month."""
    full = _gold(lake)
    write_deltalake(lake.gold, full.filter(pc.not_equal(full["month"], M)), mode="overwrite",
                    partition_by=["month"])
    _append(lake, _m(full, M).slice(0, 2))
    _append(lake, _m(full, OTHER).slice(0, 0))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    assert ev["status"] in ("RECOVERED", "ALREADY_RECOVERED")
    assert _m(_gold(lake), M).num_rows == _m(full, M).num_rows


# ------------------------------------------------------------ C. anti-join restore
def test_case_distinct_ids_are_collapsed_by_normalisation(tmp_path, small_ds):
    """ADVERSARY: two genuinely different source transactions whose ids differ only by case
    ('T00000001' and 't00000001') are collapsed into one by silver; one transaction's money vanishes and
    no check flags the key collision."""
    ds = small_ds
    extra = dict(ds.transactions[0])
    extra["txn_id"] = ds.transactions[0]["txn_id"].lower()
    extra["amount_local"] = Decimal("123.45")
    ds2 = type(ds)(transactions=ds.transactions + [extra], fx=ds.fx, accounts=ds.accounts, months=ds.months)
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, ds2)
    suite = dq.run_suite(lk)
    rec = recon.reconcile(_gold(lk), recon.read(lk.summary))
    # data loss must surface somewhere, ideally as an explicit key-collision finding
    assert any("collision" in json.dumps(c).lower() for c in suite["checks"]), \
        f"collision not named; suite passed={suite['passed']} breaks={rec['break_count']}"


def test_restore_skips_row_whose_id_exists_with_different_amount(lake, tmp_path):
    """ADVERSARY: after the incident someone reloads one txn of the month with a WRONG amount. The anti-join
    sees the id as present and does not restore the correct row; recover must not report success (defence check)."""
    full = _gold(lake)
    bad_load(lake, M)
    row = _m(full, M).slice(0, 1).to_pylist()[0]
    row["amount_usd"] = row["amount_usd"] + Decimal("1.00")
    _append(lake, pa.Table.from_pylist([row], schema=full.schema))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    assert ev["status"] not in ("RECOVERED", "ALREADY_RECOVERED")


# ------------------------------------------------------------ D. journal / resume
def test_double_resume_after_crash_is_idempotent(lake, tmp_path):
    """ADVERSARY: crash after restore, then resume twice: both resumes report ALREADY_RECOVERED and the month
    has exactly the pre-incident rows (defence check)."""
    full = _gold(lake)
    bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk",
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    for i in (1, 2):
        ev = recover(lake, M, tmp_path / f"res{i}", tmp_path / f"bk{i}", write_evidence=False)
        assert ev["status"] == "ALREADY_RECOVERED"
    assert _month_ids(lake, M) == sorted(_m(full, M)["txn_id"].to_pylist())


def test_crash_after_corrupt_restore_is_never_rolled_back(lake, tmp_path):
    """ADVERSARY: a restore batch that is wrong (written twice) AND a crash before verify. The crash path
    only re-raises; no rollback. Resume then aborts in the dry run (V04/V06) and the corrupt rows stay forever."""
    full = _gold(lake)
    bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk",
                corrupt_restore=lambda b: pa.concat_tables([b, b]),
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    recover(lake, M, tmp_path / "res2", tmp_path / "bk2", write_evidence=False)
    assert _m(_gold(lake), M).num_rows in (0, _m(full, M).num_rows), \
        f"month left with {_m(_gold(lake), M).num_rows} rows (pre-incident {_m(full, M).num_rows})"


def test_crash_right_after_append_writes_evidence(lake, tmp_path, monkeypatch):
    """ADVERSARY: a crash between write_deltalake and the journal step (outside the try) leaves no
    CRASHED_AFTER_RESTORE_UNVERIFIED evidence and no journal entry for the write."""
    bad_load(lake, M)
    real = R.write_deltalake

    def write_then_die(*a, **k):
        real(*a, **k)
        raise KeyboardInterrupt("killed right after commit")
    monkeypatch.setattr(R, "write_deltalake", write_then_die)
    with pytest.raises(KeyboardInterrupt):
        recover(lake, M, tmp_path / "res", tmp_path / "bk")
    ev = tmp_path / "res" / "recovery_evidence.json"
    assert ev.exists() and json.loads(ev.read_text()).get("status") == "CRASHED_AFTER_RESTORE_UNVERIFIED"


def test_crash_during_rollback_writes_evidence(lake, tmp_path, monkeypatch):
    """ADVERSARY: if the Delta RESTORE itself fails, the exception escapes with no evidence of a failed live verify
    and a corrupt table."""
    bad_load(lake, M)

    def boom(self, *a, **k):
        raise OSError("disk full during restore")
    monkeypatch.setattr(DeltaTable, "restore", boom)
    try:
        recover(lake, M, tmp_path / "res", tmp_path / "bk", corrupt_restore=lambda b: pa.concat_tables([b, b]))
    except OSError:
        pass
    ev = tmp_path / "res" / "recovery_evidence.json"
    assert ev.exists() and "ROLLBACK" in json.loads(ev.read_text()).get("status", "").upper()


def test_resume_in_same_results_dir_keeps_crash_history(lake, tmp_path):
    """ADVERSARY: resuming into the same results dir overwrites the journal and the evidence; the crash
    record (audit trail) is lost."""
    bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk",
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    recover(lake, M, tmp_path / "res", tmp_path / "bk")
    j = (tmp_path / "res" / "recovery_journal.json").read_text()
    assert "crashed" in j


def test_tampered_journal_does_not_change_resume(lake, tmp_path):
    """ADVERSARY: a garbage journal must not break or steer resume (defence check: the journal is write-only)."""
    bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk",
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    (tmp_path / "res" / "recovery_journal.json").write_text('{"step":"7_downstream_checks","status":"RECOVERED"')
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk2", write_evidence=False)
    assert ev["status"] == "ALREADY_RECOVERED"


# ------------------------------------------------------------ E. FX false positives
def _with_fx(tmp_path, small_ds, ccy, rates):
    lk = Lake(tmp_path / "lake")
    pipeline.load_bronze(lk, small_ds)
    fx = recon.read(lk.bronze_fx).to_pylist()
    for r in fx:
        if r["currency"] == ccy and r["month"] in rates:
            r["rate_to_usd"] = rates[r["month"]]
    write_deltalake(lk.bronze_fx, pa.Table.from_pylist(fx, schema=pipeline.FX_SCHEMA), mode="overwrite")
    pipeline.build_silver(lk)
    pipeline.build_gold(lk)
    return lk


def test_legit_devaluation_is_not_a_permanent_dq_failure(tmp_path, small_ds):
    """ADVERSARY: a real 15% devaluation of SEK from month 2 onwards (common for volatile currencies) makes the
    DQ suite FAIL for every month after it: a false positive that blocks 'RECOVERED' status."""
    lk = _with_fx(tmp_path, small_ds, "SEK", {"2025-02": "0.080000", "2025-03": "0.080500", "2025-04": "0.079800"})
    san = dq.currency_sanity(_gold(lk), recon.read(lk.bronze_fx))
    later = [p for p in san["problems"] if "2025-03" in p or "2025-04" in p]
    assert not later, later


def test_currency_at_parity_is_false_fallback(tmp_path, small_ds):
    """ADVERSARY: CHF legitimately trading at 0.9995 USD is flagged as a fallback-to-1 (false positive).
    README lists this as a limitation, so it is MEDIUM, accepted."""
    lk = _with_fx(tmp_path, small_ds, "CHF", {"2025-02": "0.999500"})
    assert dq.fx_fallback_to_one(_gold(lk), recon.read(lk.bronze_fx))["passed"]


# ------------------------------------------------------------ F. V08
def test_v08_blocks_recovery_after_legit_commit_to_other_month(lake, tmp_path):
    """ADVERSARY: (rewritten in round 3 per orchestrator ruling) one late posting to another month, absent from
    the control, must give a clear, specific refusal - not RECOVERED, not a generic dry-run failure - whose
    reason names the unreconciled late posting, and the evidence must be written."""
    full = _gold(lake)
    bad_load(lake, M)
    row = _m(full, OTHER).slice(0, 1).to_pylist()[0]
    row["txn_id"] = "LATE-POST-1"
    _append(lake, pa.Table.from_pylist([row], schema=full.schema))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk")
    assert ev["status"].startswith("ABORTED") and ev["status"] != "ABORTED_DRY_RUN_FAILED", ev["status"]
    written = (tmp_path / "res" / "recovery_evidence.json")
    assert written.exists()
    reason = json.dumps({k: ev.get(k) for k in ("reason", "error", "unreconciled")}, default=str)
    assert "LATE-POST-1" in reason or OTHER in reason, reason


# ------------------------------------------------------------ G. rollback-proof demo
def test_rollback_proof_in_results_is_meaningful():
    """ADVERSARY: the recorded rollback proof must show a failed live verify, a real RESTORE to the pre-restore
    version, and row count equal to source minus the deleted month (defence check)."""
    r = json.loads((ROOT / "results" / "demo_results.json").read_text())
    rp = r["rollback_proof"]
    assert rp["status"] == "ROLLED_BACK" and rp["failed_live_tests"]
    assert rp["rollback"]["matches_pre_restore_state"] is True
    assert rp["rollback"]["rows_after_rollback"] == r["dataset"]["transactions"] - r["incident_b"]["rows_deleted"]
    assert rp["rollback"]["new_version"] > rp["rollback"]["restored_to_version"]


def test_rollback_proof_evidence_file_agrees():
    """ADVERSARY: results/rollback_proof/recovery_evidence.json must agree with the demo summary (defence check)."""
    r = json.loads((ROOT / "results" / "demo_results.json").read_text())["rollback_proof"]
    ev = json.loads((ROOT / "results" / "rollback_proof" / "recovery_evidence.json").read_text())
    assert ev["status"] == r["status"] and ev["rollback"] == r["rollback"]


# ------------------------------------------------------------ H. README vs results
def _readme_table():
    txt = (ROOT / "README.md").read_text(encoding="utf-8")
    return dict(re.findall(r"^\| ([^|]+?) \| ([^|]+?) \|$", txt, re.M))


def test_readme_test_counts_match_latest_recorded_run():
    """ADVERSARY: (tightened in round 3) the README's builder/adversary results must equal the LATEST recorded
    run of each suite in the append-only results/test_runs.txt, including failures - not any historic line."""
    t = _readme_table()
    runs = (ROOT / "results" / "test_runs.txt").read_text()
    latest = {}
    for label, tail in re.findall(r"^## (builder|adversary)\b.*\n\$ .*\n(.*)$", runs, re.M):
        latest[label] = tail
    adv_key = next(k for k in t if k.startswith("Adversary tests"))
    for label, claim in (("builder", t["Builder tests"]), ("adversary", t[adv_key])):
        rec = latest[label]
        p = re.search(r"(\d+) passed", rec)
        f = re.search(r"(\d+) failed", rec)
        assert re.search(rf"\b{p.group(1)} passed", claim), (label, claim, rec)
        if f:
            assert re.search(rf"\b{f.group(1)}\b.*fail", claim), (label, claim, rec)
        else:
            assert "fail" not in claim.lower(), (label, claim, rec)


def test_readme_timing_matches_results():
    """ADVERSARY: 'Backup start -> live verify passing' in README equals results first_verify_pass_s (1 dp)."""
    t = _readme_table()
    r = json.loads((ROOT / "results" / "demo_results.json").read_text())["recovery"]
    assert t["Backup start -> live verify passing"] == f"{round(r['first_verify_pass_s'], 1)} s"
