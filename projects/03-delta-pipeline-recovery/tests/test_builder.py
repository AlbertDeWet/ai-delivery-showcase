"""Builder tests (written by the builder role). Adversary tests live in tests/adversarial/."""
from datetime import timedelta
from decimal import Decimal

import pyarrow.compute as pc
import pytest
from deltalake import DeltaTable, write_deltalake

from deltarecover import dedupe, dq, recon
from deltarecover.generate import generate, to_reporting
from deltarecover.pipeline import Lake, run_pipeline
from deltarecover.recover import _commit_ts, bad_load, parse_interval, recover

M = "2025-02"
REDELIVER = {("CI-E02", "2025-01"), ("CI-E05", "2025-03")}


@pytest.fixture
def lake(tmp_path, small_ds):
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, small_ds, REDELIVER)
    return lk


# ---------------- generator
def test_generator_is_deterministic():
    a, b = generate(seed=3, n_accounts=5, n_months=2), generate(seed=3, n_accounts=5, n_months=2)
    assert a.transactions == b.transactions and a.fx == b.fx


def test_generator_amounts_are_decimal_cents(small_ds):
    assert all(isinstance(t["amount_local"], Decimal) and t["amount_local"].as_tuple().exponent == -2
               for t in small_ds.transactions)


def test_to_reporting_rounds_half_up():
    assert to_reporting(Decimal("0.05"), Decimal("0.5")) == Decimal("0.03")


# ---------------- pipeline + recon
def test_clean_pipeline_reconciles_every_cell(lake):
    r = recon.reconcile(recon.read(lake.gold), recon.read(lake.summary))
    assert r["break_count"] == 0 and r["cells"] > 0 and r["diff_total_usd"] == "0.00"


def test_amount_columns_are_decimal_types(lake):
    t = recon.read(lake.gold).schema
    for col in ("amount_local", "amount_usd", "fx_rate"):
        assert str(t.field(col).type).startswith("decimal"), col


def test_recon_detects_one_cent_break(lake):
    g = recon.read(lake.gold)
    rows = g.to_pylist()
    rows[0]["amount_usd"] += Decimal("0.01")
    import pyarrow as pa
    r = recon.reconcile(pa.Table.from_pylist(rows, schema=g.schema), recon.read(lake.summary))
    assert r["break_count"] == 1 and r["diff_total_usd"] == "0.01"


# ---------------- incident A
def test_dedupe_bug_doubles_exactly_the_redelivered_months(tmp_path, small_ds):
    lk = Lake(tmp_path / "a")
    run_pipeline(lk, small_ds, REDELIVER, dedupe_bug=True)
    d = dedupe.doubled_entity_months(recon.read(lk.gold), recon.read(lk.summary))
    assert {(x["entity"], x["month"]) for x in d["doubled"]} == REDELIVER
    assert d["other_mismatch"] == []


def test_fix_removes_all_doubles(lake):
    d = dedupe.doubled_entity_months(recon.read(lake.gold), recon.read(lake.summary))
    assert d["doubled_count"] == 0 and d["duplicate_txn_ids"] == 0 and d["other_mismatch"] == []


# ---------------- DQ
def test_dq_clean_passes(lake):
    assert dq.run_suite(lake)["passed"]


def test_dq_detects_fx_fallback_to_one(tmp_path, small_ds):
    lk = Lake(tmp_path / "fx")
    run_pipeline(lk, small_ds, drop_fx={("EUR", "2025-02")})
    res = dq.run_suite(lk)
    fx = next(c for c in res["checks"] if c["check"] == "fx_fallback_to_one")
    assert not fx["passed"]
    assert fx["missing_rates"] == [{"currency": "EUR", "month": "2025-02"}]
    assert fx["rate_one_groups"][0]["currency"] == "EUR"


def test_dq_detects_null_key(lake):
    g = recon.read(lake.gold)
    rows = g.to_pylist()
    rows[0]["account"] = None
    import pyarrow as pa
    assert not dq.null_keys(pa.Table.from_pylist(rows, schema=g.schema))["passed"]


def test_dq_lineage_breaks_when_gold_loses_rows(lake):
    DeltaTable(lake.gold).delete(f"month = '{M}'")
    assert not dq.lineage(lake)["passed"]


def test_currency_sanity_flags_reporting_rate_not_one(lake):
    fx = recon.read(lake.bronze_fx).to_pylist()
    for r in fx:
        if r["currency"] == "USD":
            r["rate_to_usd"] = "1.01"
    import pyarrow as pa
    res = dq.currency_sanity(recon.read(lake.gold), pa.Table.from_pylist(fx))
    assert not res["passed"]


# ---------------- incident B
def test_parse_interval():
    assert parse_interval("interval 7 days") == timedelta(days=7)
    with pytest.raises(ValueError):
        parse_interval("7d")


def test_recover_restores_and_reconciles(lake, tmp_path):
    n0 = recon.read(lake.gold).num_rows
    inc = bad_load(lake, M)
    deleted = n0 - recon.read(lake.gold).num_rows
    assert deleted > 0
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk")
    assert ev["status"] == "RECOVERED"
    assert ev["restored_rows"] == deleted
    assert len(ev["dry_run"]) == 12 and all(t["passed"] for t in ev["dry_run"])
    assert ev["reconciliation"]["break_count"] == 0
    assert ev["pre_incident_version"] == inc["pre_incident_version"]
    assert (tmp_path / "res" / "recovery_evidence.json").exists()
    assert (tmp_path / "res" / "recovery_evidence.md").exists()
    assert recon.read(lake.gold).num_rows == n0


def test_recover_aborts_when_retention_expired(lake, tmp_path):
    inc = bad_load(lake, M)
    late = _commit_ts(DeltaTable(lake.gold), inc["incident_version"]) + timedelta(days=8)
    v = DeltaTable(lake.gold).version()
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", now=late)
    assert ev["status"] == "ABORTED_RETENTION_EXPIRED"
    assert DeltaTable(lake.gold).version() == v  # nothing written


def test_hours_remaining_is_computed(lake, tmp_path):
    inc = bad_load(lake, M)
    now = _commit_ts(DeltaTable(lake.gold), inc["incident_version"]) + timedelta(days=5)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", now=now, write_evidence=False)
    assert ev["retention"]["hours_remaining"] == 48.0


def test_recover_rolls_back_on_live_verify_failure(lake, tmp_path):
    bad_load(lake, M)
    before = recon.cells(recon.read(lake.gold))

    import pyarrow as pa
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk",
                 corrupt_restore=lambda t: pa.concat_tables([t, t.slice(0, 1)]))
    assert ev["status"] == "ROLLED_BACK"
    assert ev["rollback"]["matches_pre_restore_state"]
    assert recon.cells(recon.read(lake.gold)) == before


def test_rollback_refused_when_foreign_commit_follows(lake, tmp_path):
    full = recon.read(lake.gold)
    bad_load(lake, M)
    import pyarrow as pa

    def other_writer(lk):
        write_deltalake(lk.gold, full.filter(pc.equal(full["month"], "2025-03")).slice(0, 1), mode="append",
                        partition_by=["month"])
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", inject_fault=other_writer,
                 corrupt_restore=lambda t: pa.concat_tables([t, t.slice(0, 1)]))
    assert ev["status"] == "ROLLBACK_REFUSED_CONCURRENT_WRITES" and ev["rollback"]["refused"]


def test_month_validation_rejects_bad_values(lake):
    for bad in ("2025-13", "2025-1", "25-01", "2025-01 ", None):
        with pytest.raises(ValueError):
            bad_load(lake, bad)


def test_parse_interval_forms():
    assert parse_interval("interval 1 weeks") == timedelta(weeks=1)
    assert parse_interval("INTERVAL 36 HOURS") == timedelta(hours=36)
    assert parse_interval("interval 1 day 12 hours") == timedelta(hours=36)
    assert parse_interval("interval 90 minutes") == timedelta(minutes=90)


def test_control_rounding_matches_half_up_reference():
    from decimal import ROUND_HALF_UP
    from deltarecover.control import control_round_usd
    import random
    rng = random.Random(1)
    for _ in range(2000):
        a = Decimal(rng.randint(-10**9, 10**9)) / 100
        r = Decimal(rng.randint(1, 3_000_000)) / 10**6
        assert control_round_usd(a, r) == (a * r).quantize(Decimal("0.01"), ROUND_HALF_UP), (a, r)


def test_crash_writes_evidence_and_rerun_is_idempotent(lake, tmp_path):
    bad_load(lake, M)

    def crash(_):
        raise RuntimeError("killed")
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk", inject_fault=crash)
    import json
    ev = json.loads((tmp_path / "res" / "recovery_evidence.json").read_text())
    assert ev["status"] == "CRASHED_AFTER_RESTORE_UNVERIFIED"
    assert (tmp_path / "res" / "recovery_journal.json").exists()
    assert recover(lake, M, tmp_path / "r2", tmp_path / "b2")["status"] == "ALREADY_RECOVERED"


def test_ambiguous_incident_is_refused_and_explicit_version_works(lake, tmp_path):
    full = recon.read(lake.gold)
    first = bad_load(lake, M)                                        # v1 incident A
    write_deltalake(lake.gold, full.filter(pc.equal(full["month"], M)), mode="append", partition_by=["month"])
    bad_load(lake, M)                                                # v3 incident B (same month)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    assert ev["status"] == "ABORTED_AMBIGUOUS_INCIDENT"
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=first["incident_version"],
                 write_evidence=False)
    assert ev["pre_incident_version"] == 0 and ev["incident_source"] == "operator"


def test_resume_rolls_back_corrupt_batch_then_recovers(lake, tmp_path):
    import pyarrow as pa
    full = recon.read(lake.gold)
    bad_load(lake, M)
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk", corrupt_restore=lambda b: pa.concat_tables([b, b]),
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk2")
    assert ev["resume_cleanup"]["action"] == "rolled_back" and ev["status"] == "RECOVERED"
    m = recon.read(lake.gold)
    assert m.filter(pc.equal(m["month"], M)).num_rows == full.filter(pc.equal(full["month"], M)).num_rows
    import json
    steps = [e["step"] for e in json.loads((tmp_path / "res" / "recovery_journal.json").read_text())]
    assert steps.index("4_restore_intent") < steps.index("crashed") < steps.index("resume_cleanup_rolled_back")
    assert len(list((tmp_path / "res" / "runs").glob("*.json"))) == 2


def test_id_collision_check(lake, tmp_path, small_ds):
    assert next(c for c in dq.run_suite(lake)["checks"] if c["check"] == "id_collision")["passed"]
    extra = dict(small_ds.transactions[0], txn_id=small_ds.transactions[0]["txn_id"].lower(),
                 amount_local=Decimal("1.00"))
    ds2 = type(small_ds)(transactions=small_ds.transactions + [extra], fx=small_ds.fx,
                         accounts=small_ds.accounts, months=small_ds.months)
    lk = Lake(tmp_path / "c")
    run_pipeline(lk, ds2)
    c = next(c for c in dq.run_suite(lk)["checks"] if c["check"] == "id_collision")
    assert not c["passed"] and c["collisions"] == 1


def test_cleanup_refuses_registered_batch_that_touches_other_months(lake, tmp_path):
    """A batch id IS in our registry, but its commit also changed another month: refuse, never discard."""
    from deltalake import CommitProperties
    from deltarecover.recover import BATCH_KEY, _register_batch
    import pyarrow as pa
    full = recon.read(lake.gold)
    inc = bad_load(lake, M)
    _register_batch(lake, "ours-1", M, "test", 2)
    bad = full.filter(pc.equal(full["month"], M)).slice(0, 1).to_pylist()[0]
    bad["amount_usd"] += Decimal("1.00")
    other = dict(full.filter(pc.equal(full["month"], "2025-03")).slice(0, 1).to_pylist()[0], txn_id="KEEP-ME")
    write_deltalake(lake.gold, pa.Table.from_pylist([bad, other], schema=full.schema), mode="append",
                    partition_by=["month"], commit_properties=CommitProperties(custom_metadata={BATCH_KEY: "ours-1"}))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    assert ev["status"] == "ABORTED_BATCH_TOUCHES_OTHER_MONTHS"
    assert "KEEP-ME" in set(recon.read(lake.gold)["txn_id"].to_pylist())


def test_late_posting_refusal_names_commit_and_rows(lake, tmp_path):
    import pyarrow as pa
    full = recon.read(lake.gold)
    inc = bad_load(lake, M)
    row = dict(full.filter(pc.equal(full["month"], "2025-03")).slice(0, 1).to_pylist()[0], txn_id="LATE-9")
    write_deltalake(lake.gold, pa.Table.from_pylist([row], schema=full.schema), mode="append", partition_by=["month"])
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    assert ev["status"] == "ABORTED_UNRECONCILED_LATER_COMMIT"
    assert ev["later_commit"]["months"] == ["2025-03"] and "LATE-9" in ev["later_commit"]["new_txn_ids"]
    assert ev["later_commit"]["later_commits"] == [inc["incident_version"] + 1]


def test_journal_concurrent_threads_lose_nothing(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from deltarecover.recover import Journal
    import json
    js = [Journal(tmp_path, {"run_id": f"R{i}"}) for i in range(4)]
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda k: js[k % 4].step(f"s{k}"), range(200)))
    data = json.loads((tmp_path / "recovery_journal.json").read_text())
    assert len(data) == 200 and {e["run_id"] for e in data} == {"R0", "R1", "R2", "R3"}


def test_recon_is_thread_safe():
    from concurrent.futures import ThreadPoolExecutor
    import pyarrow as pa
    tabs = [pa.table({"entity": [f"E{i}"] * (i + 1), "account": ["1"] * (i + 1), "month": [M] * (i + 1),
                      "amount_usd": pa.array([Decimal("1.00")] * (i + 1), pa.decimal128(18, 2))}) for i in range(6)]
    exp = [recon.cells(t) for t in tabs]
    with ThreadPoolExecutor(6) as ex:
        ok = list(ex.map(lambda k: recon.cells(tabs[k % 6]) == exp[k % 6], range(300)))
    assert all(ok)


def test_lock_released_on_every_acquire_error(tmp_path, monkeypatch):
    from deltarecover import recover as R
    lock = R._FileLock(tmp_path / "j.json", timeout=0.2)

    def boom(*a, **k):
        raise OSError("antivirus holds the file")
    monkeypatch.setattr(R.os, "open", boom)
    with pytest.raises(OSError):
        with lock:
            pass
    assert not R._FileLock._THREAD_LOCK.locked()


def test_invalid_registry_is_refused_with_evidence(lake, tmp_path):
    import json
    from deltarecover.recover import _registry
    inc = bad_load(lake, M)
    _registry(lake).write_text('{"not": "an array"}')
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    assert ev["status"] == "ABORTED_REGISTRY_INVALID"
    assert json.loads((tmp_path / "res" / "recovery_evidence.json").read_text())["status"] == ev["status"]


@pytest.mark.parametrize("bad", [True, 1.0, "1", None.__class__])
def test_incident_version_must_be_a_real_int(lake, tmp_path, bad):
    bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=bad, write_evidence=False)
    assert ev["status"] == "ABORTED_INCIDENT_VERSION_INVALID"


def test_constant_near_parity_currency_fails_any_history(lake):
    import pyarrow as pa
    fx = recon.read(lake.bronze_fx).to_pylist()
    for r in fx:
        if r["currency"] == "EUR":
            r["rate_to_usd"] = "1.000200"
    res = dq.fx_fallback_to_one(recon.read(lake.gold), pa.Table.from_pylist(fx))
    assert not res["passed"] and res["constant_near_parity"] == ["EUR"]


def test_backup_is_byte_identical(lake, tmp_path):
    bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", write_evidence=False)
    assert ev["backup"]["manifest_match"] and ev["backup"]["files"] > 0
