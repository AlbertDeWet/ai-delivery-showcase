"""ADVERSARY tests, round 4. Written by the adversarial agent, not the builder.

A FAILING test proves an open break. A PASSING test proves the builder's defence held.
Fast: tiny dataset, and lock timeouts are shortened via monkeypatch.
"""
import json
import os
import threading
import time
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


@pytest.fixture(scope="module")
def tiny_ds():
    return generate(seed=13, n_accounts=4, n_months=3, max_txn=3)


@pytest.fixture
def lake(tmp_path, tiny_ds):
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, tiny_ds)
    return lk


@pytest.fixture
def short_locks(monkeypatch):
    monkeypatch.setattr(R._FileLock.__init__, "__defaults__", (0.5,))


def _gold(lk):
    return recon.read(lk.gold)


def _m(t, month):
    return t.filter(pc.equal(t["month"], month))


def _append(lk, t, meta=None):
    kw = {"commit_properties": CommitProperties(custom_metadata=meta)} if meta else {}
    write_deltalake(lk.gold, t.cast(_gold(lk).schema), mode="append", partition_by=["month"], **kw)


def _reg(lk):
    return Path(lk.root) / "_deltarecover" / "batch_registry.json"


def _crash_with_doubled_batch(lake, tmp_path, inc):
    with pytest.raises(RuntimeError):
        recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                corrupt_restore=lambda b: pa.concat_tables([b, b]),
                inject_fault=lambda _: (_ for _ in ()).throw(RuntimeError("kill")))


# ------------------------------------------------------------ A. registry
def test_deleted_registry_makes_resume_refuse_not_destroy(lake, tmp_path):
    """ADVERSARY: registry deleted after a crash with a doubled batch: resume must refuse with a specific status,
    never roll back an unrecognised commit (defence check)."""
    inc = bad_load(lake, M)
    _crash_with_doubled_batch(lake, tmp_path, inc)
    _reg(lake).unlink()
    v = DeltaTable(lake.gold).version()
    ev = recover(lake, M, tmp_path / "r2", tmp_path / "b2", incident_version=inc["incident_version"])
    assert ev["status"] == "ABORTED_UNRECOGNISED_BATCH" and DeltaTable(lake.gold).version() == v


def test_corrupt_registry_does_not_crash_recover(lake, tmp_path):
    """ADVERSARY: a registry file that is not a JSON array ('{not json') makes _register_batch raise ValueError
    OUTSIDE the restore try-block: recover crashes with no status and no evidence instead of refusing."""
    inc = bad_load(lake, M)
    _reg(lake).parent.mkdir(parents=True, exist_ok=True)
    _reg(lake).write_text("{not json")
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    except ValueError:
        ev = None
    assert ev is not None and ev["status"] in ("RECOVERED",) or (ev and ev["status"].startswith("ABORTED")), \
        "crash on a corrupt registry"
    assert (tmp_path / "res" / "recovery_evidence.json").exists()


def test_registry_entry_for_nonexistent_commit_is_harmless(lake, tmp_path):
    """ADVERSARY: registry entries whose batch never committed must not change a normal recovery (defence check)."""
    inc = bad_load(lake, M)
    R._register_batch(lake, "ghost-batch", M, "ghost-run", 999)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"] == "RECOVERED"


def test_forged_registry_entry_cannot_reach_other_months(lake, tmp_path):
    """ADVERSARY: even with a forged registry entry, a tagged foreign commit that also touches another month is
    refused and the foreign row survives (defence check: the touches-only guard)."""
    full = _gold(lake)
    inc = bad_load(lake, M)
    bad = _m(full, M).slice(0, 1).to_pylist()[0]
    bad["amount_usd"] += Decimal("5.00")
    legit = _m(full, OTHER).slice(0, 1).to_pylist()[0]
    legit["txn_id"] = "FOREIGN-LEGIT-2"
    R._register_batch(lake, "forged", M, "attacker", 2)
    _append(lake, pa.Table.from_pylist([bad, legit], schema=full.schema), meta={BATCH_KEY: "forged"})
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"] == "ABORTED_BATCH_TOUCHES_OTHER_MONTHS"
    assert "FOREIGN-LEGIT-2" in set(_gold(lake)["txn_id"].to_pylist())


def test_registry_valid_json_but_wrong_shape_keeps_array_valid(tmp_path):
    """ADVERSARY: a registry holding a JSON OBJECT whose tail ends in ']' ({"a": [1]}) is 'appended' to by
    byte-splicing before the last ']', producing invalid JSON that silently disables every later lookup."""
    p = tmp_path / "batch_registry.json"
    p.write_text('{"a": [1]}')
    try:
        R._append_json_array(p, {"batch_id": "x"})
    except ValueError:
        return  # refusing a non-array is fine
    json.loads(p.read_text())  # must still parse


# ------------------------------------------------------------ B. lock files
def test_fresh_orphan_lock_blocks_then_errors_cleanly(tmp_path, short_locks):
    """ADVERSARY: a lock left by a crashed process with a FUTURE mtime (clock skew / copied file) is never 'stale';
    the journal step must fail with a clear TimeoutError, not hang (defence check)."""
    j = Journal(tmp_path, {"run_id": "a"})
    lock = Path(str(j.path) + ".lock")
    lock.write_text("")
    future = time.time() + 3600
    os.utime(lock, (future, future))
    t = time.perf_counter()
    with pytest.raises(TimeoutError):
        j.step("x")
    assert time.perf_counter() - t < 5


def test_stale_lock_is_broken(tmp_path, short_locks):
    """ADVERSARY: an old orphan lock (crash) is broken and the step succeeds (defence check)."""
    j = Journal(tmp_path, {"run_id": "a"})
    lock = Path(str(j.path) + ".lock")
    lock.write_text("")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    j.step("x")
    assert json.loads(j.path.read_text())[-1]["step"] == "x" and not lock.exists()


def test_lock_error_does_not_deadlock_the_process(tmp_path, short_locks):
    """ADVERSARY: if __enter__ raises anything other than the handled errors (here: the lock path is an old
    DIRECTORY, so unlink fails), the class-wide _THREAD_LOCK is never released and every later journal or
    registry write in the process hangs forever."""
    j = Journal(tmp_path / "a", {"run_id": "a"})
    lockdir = Path(str(j.path) + ".lock")
    lockdir.mkdir()
    old = time.time() - 3600
    os.utime(lockdir, (old, old))
    try:
        j.step("x")
    except Exception:  # noqa: BLE001
        pass
    other = Journal(tmp_path / "b", {"run_id": "b"})
    done = threading.Event()
    th = threading.Thread(target=lambda: (other.step("y"), done.set()), daemon=True)
    th.start()
    th.join(5)
    if not done.is_set() and R._FileLock._THREAD_LOCK.locked():
        R._FileLock._THREAD_LOCK.release()  # unblock the daemon thread so the rest of the session survives
    assert done.is_set(), "process-wide lock leaked: unrelated journal deadlocked"


def test_lock_file_is_removed_after_each_step(tmp_path):
    """ADVERSARY: no lock file is left behind after a normal step (defence check)."""
    j = Journal(tmp_path, {"run_id": "a"})
    j.step("x")
    assert not Path(str(j.path) + ".lock").exists()


# ------------------------------------------------------------ C. UTF-8 evidence
def test_evidence_with_non_ascii_paths_and_errors(lake, tmp_path):
    """ADVERSARY: results dir and error text with non-cp1252 characters; evidence (json + md) is valid UTF-8 (defence check)."""
    inc = bad_load(lake, M)
    res = tmp_path / "résultats_✓_证据"
    ev = recover(lake, M, res, tmp_path / "bk", incident_version=inc["incident_version"] + 99)
    assert ev["status"] == "ABORTED_INCIDENT_VERSION_INVALID"
    assert json.loads((res / "recovery_evidence.json").read_text(encoding="utf-8"))["status"] == ev["status"]
    (res / "recovery_evidence.md").read_text(encoding="utf-8")


# ------------------------------------------------------------ D. incident_version validation
@pytest.mark.parametrize("bad", [0, -1, 10_000, "x", [1]])
def test_incident_version_garbage_refused_with_evidence(lake, tmp_path, bad):
    """ADVERSARY: out-of-range, negative, non-numeric and non-scalar incident versions give the specific refusal (defence check)."""
    bad_load(lake, M)
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=bad)
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"crashed: {e!r}")
    assert ev["status"] == "ABORTED_INCIDENT_VERSION_INVALID"


def test_incident_version_float_is_not_truncated(lake, tmp_path):
    """ADVERSARY: incident_version=1.9 is silently truncated by int() to 1 and accepted; a non-integral version
    is an operator error and must be refused."""
    inc = bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"] + 0.9,
                 write_evidence=False)
    assert ev["status"] == "ABORTED_INCIDENT_VERSION_INVALID", ev["status"]


def test_incident_version_that_did_not_empty_month_refused(lake, tmp_path):
    """ADVERSARY: a real commit that did not empty the month is refused as invalid (defence check, R3-09)."""
    inc = bad_load(lake, M)
    _append(lake, _m(_gold(lake), OTHER).slice(0, 0))
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"] + 1,
                 write_evidence=False)
    assert ev["status"] == "ABORTED_INCIDENT_VERSION_INVALID"


# ------------------------------------------------------------ E. near-1 FX rule at the 3-month boundary
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


def _dq_passes(lk):
    g, fx = _gold(lk), recon.read(lk.bronze_fx)
    return dq.fx_fallback_to_one(g, fx)["passed"] and dq.currency_sanity(g, fx)["passed"]


@pytest.mark.parametrize("n", [2, 3])
def test_single_disguised_month_at_boundary(tmp_path, n):
    """ADVERSARY: one disguised 1.0001 EUR month is caught at 2 months (short-history rule) and at 3 months
    (outlier vs history) (defence check at the boundary)."""
    lk = _fx_lake(tmp_path, n, {("EUR", f"2025-0{n}"): "1.000100"})
    assert not _dq_passes(lk)


def test_all_months_disguised_at_three_month_boundary(tmp_path):
    """ADVERSARY: at exactly 3 months, EUR at 1.0001 in EVERY month passes DQ: the short-history rule stops at 3,
    near-1 becomes info only, the median IS 1.0001 so there is no outlier, and there is no jump."""
    lk = _fx_lake(tmp_path, 3, {("EUR", f"2025-0{i}"): "1.000100" for i in (1, 2, 3)})
    assert not _dq_passes(lk), "a currency stuck at a disguised fallback for 3 months is not detected"


def test_two_of_three_disguised_blames_the_right_month(tmp_path):
    """ADVERSARY: with 2 of 3 months disguised, the median moves to 1.0001 and the GOOD month is reported as the
    outlier; the disguised months are not named."""
    lk = _fx_lake(tmp_path, 3, {("EUR", "2025-02"): "1.000100", ("EUR", "2025-03"): "1.000100"})
    san = dq.currency_sanity(_gold(lk), recon.read(lk.bronze_fx))
    fx1 = dq.fx_fallback_to_one(_gold(lk), recon.read(lk.bronze_fx))
    named = json.dumps(san) + json.dumps(fx1.get("near_one_short_history", []))
    assert "EUR 2025-02" in named or "EUR 2025-03" in named, san["problems"]
