"""ADVERSARY tests, round 5 (final focused attack on the R4 fixes). Written by the adversarial agent.

A FAILING test proves an open break. A PASSING test proves the builder's defence held.
"""
import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest
from deltalake import write_deltalake

from deltarecover import dq, pipeline, recon
from deltarecover import recover as R
from deltarecover.generate import generate
from deltarecover.pipeline import Lake, run_pipeline
from deltarecover.recover import Journal, bad_load, recover

M = "2025-02"


@pytest.fixture(scope="module")
def tiny_ds():
    return generate(seed=17, n_accounts=4, n_months=3, max_txn=3)


@pytest.fixture
def lake(tmp_path, tiny_ds):
    lk = Lake(tmp_path / "lake")
    run_pipeline(lk, tiny_ds)
    return lk


def _reg(lk):
    p = Path(lk.root) / "_deltarecover" / "batch_registry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _lock_free_within(seconds=5.0) -> bool:
    ok = R._FileLock._THREAD_LOCK.acquire(timeout=seconds)
    if ok:
        R._FileLock._THREAD_LOCK.release()
    return ok


# ------------------------------------------------------------ A. KeyboardInterrupt during lock acquisition
def test_interrupt_in_os_open_releases_thread_lock(tmp_path, monkeypatch):
    """ADVERSARY: KeyboardInterrupt raised inside os.open while acquiring the file lock must release the
    process-wide thread lock, and a later step must work."""
    j = Journal(tmp_path, {"run_id": "a"})

    def ki(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(R.os, "open", ki)
    with pytest.raises(KeyboardInterrupt):
        j.step("x")
    monkeypatch.undo()
    assert _lock_free_within()
    j.step("y")
    assert json.loads(j.path.read_text())[-1]["step"] == "y"


def test_interrupt_while_waiting_on_held_lock(tmp_path, monkeypatch):
    """ADVERSARY: KeyboardInterrupt during the wait loop (time.sleep) while another process holds the lock file:
    the thread lock is released and the OTHER holder's lock file is not deleted."""
    j = Journal(tmp_path, {"run_id": "a"})
    lock = Path(str(j.path) + ".lock")
    lock.write_text("")

    def ki(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(R.time, "sleep", ki)
    with pytest.raises(KeyboardInterrupt):
        j.step("x")
    monkeypatch.undo()
    assert _lock_free_within() and lock.exists()
    lock.unlink()


def test_interrupt_inside_locked_append_leaves_valid_array(tmp_path, monkeypatch):
    """ADVERSARY: an interrupt after the lock is taken (inside the append) releases the lock, removes the lock file,
    and leaves the journal a valid JSON array."""
    j = Journal(tmp_path, {"run_id": "a"})
    j.step("first")
    real = Path.open

    def ki_open(self, *a, **k):
        if self.name == "recovery_journal.json" and a and "r+b" in a[0]:
            raise KeyboardInterrupt
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "open", ki_open)
    with pytest.raises(KeyboardInterrupt):
        j.step("second")
    monkeypatch.undo()
    assert _lock_free_within() and not Path(str(j.path) + ".lock").exists()
    assert [e["step"] for e in json.loads(j.path.read_text())] == ["first"]


# ------------------------------------------------------------ B. registry validation edges
@pytest.mark.parametrize("content", ["[]", "[\n]\n", "  [ ]  "])
def test_empty_array_registry_is_valid_and_appendable(lake, tmp_path, content):
    """ADVERSARY: an empty-array registry (several spellings) is valid and recovery proceeds (defence check)."""
    _reg(lake).write_text(content)
    inc = bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"] == "RECOVERED"
    assert isinstance(json.loads(_reg(lake).read_text()), list)


def test_valid_registry_with_long_trailing_whitespace(lake, tmp_path):
    """ADVERSARY: '[]' followed by 200 newlines is valid JSON, so validation passes, but the append only searches
    the last 64 bytes for ']' and raises ValueError at _register_batch; recover must refuse or recover, never crash."""
    _reg(lake).write_text("[]" + "\n" * 200)
    inc = bad_load(lake, M)
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    except ValueError as e:
        pytest.fail(f"crashed: {e!r}")
    assert ev["status"] == "RECOVERED" or ev["status"].startswith("ABORTED")


def test_huge_registry_entry(lake, tmp_path):
    """ADVERSARY: a registry with one 5 MB entry is still valid and appendable (defence check)."""
    _reg(lake).write_text(json.dumps([{"batch_id": "big", "month": M, "pad": "x" * 5_000_000}]))
    inc = bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"],
                 write_evidence=False)
    assert ev["status"] == "RECOVERED" and len(json.loads(_reg(lake).read_text())) == 2


@pytest.mark.parametrize("content", ["[1, 2]", '{"a": [1]}', "\xff\xfe garbage"])
def test_bad_registry_shapes_refused_with_evidence(lake, tmp_path, content):
    """ADVERSARY: non-object entries, an object, or undecodable bytes give ABORTED_REGISTRY_INVALID with evidence (defence check)."""
    _reg(lake).write_bytes(content.encode("latin-1"))
    inc = bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=inc["incident_version"])
    assert ev["status"] == "ABORTED_REGISTRY_INVALID" and (tmp_path / "res" / "recovery_evidence.json").exists()


# ------------------------------------------------------------ C. near-parity allowlist casing
def _fx_lake(tmp_path, overrides, ccy_rename=None):
    ds = generate(seed=5, n_accounts=2, n_months=4, max_txn=1)
    lk = Lake(tmp_path / "fx")
    pipeline.load_bronze(lk, ds)
    fx = recon.read(lk.bronze_fx).to_pylist()
    for r in fx:
        if r["currency"] in overrides:
            r["rate_to_usd"] = overrides[r["currency"]]
        if ccy_rename and r["currency"] == ccy_rename[0]:
            r["currency"] = ccy_rename[1]
    write_deltalake(lk.bronze_fx, pa.Table.from_pylist(fx, schema=pipeline.FX_SCHEMA), mode="overwrite")
    pipeline.build_silver(lk)
    pipeline.build_gold(lk)
    return lk


def _passes(lk):
    g, fx = recon.read(lk.gold), recon.read(lk.bronze_fx)
    return dq.fx_fallback_to_one(g, fx)["passed"] and dq.currency_sanity(g, fx)["passed"]


def test_allowlist_lowercase_config_does_not_unflag_eur(tmp_path, monkeypatch):
    """ADVERSARY: an allowlist entry 'eur' (wrong case) must not silently allowlist EUR, and an EUR fallback stuck
    near 1 stays flagged (fail-safe direction) (defence check)."""
    monkeypatch.setattr(dq, "NEAR_PARITY_ALLOWLIST", frozenset({"eur"}))
    assert not _passes(_fx_lake(tmp_path, {"EUR": "1.000100"}))


def test_lowercase_currency_in_fx_cannot_borrow_allowlist(tmp_path, monkeypatch):
    """ADVERSARY: with CHF allowlisted, renaming the fx currency EUR -> 'chf'/'Chf' cannot launder an EUR fallback (defence check)."""
    monkeypatch.setattr(dq, "NEAR_PARITY_ALLOWLIST", frozenset({"CHF"}))
    lk = _fx_lake(tmp_path, {"EUR": "1.000100"}, ccy_rename=("EUR", "Chf"))
    assert not _passes(lk)


def test_allowlisted_peg_is_not_flagged(tmp_path, monkeypatch):
    """ADVERSARY: a genuinely allowlisted peg at 1.0001 for CHF is not a false positive (defence check)."""
    monkeypatch.setattr(dq, "NEAR_PARITY_ALLOWLIST", frozenset({"CHF"}))
    assert _passes(_fx_lake(tmp_path, {"CHF": "1.000100"}))


# ------------------------------------------------------------ D. incident_version int subclasses / numpy
def test_numpy_int_incident_version_accepted(lake, tmp_path):
    """ADVERSARY: incident_version as numpy.int64 (what pandas/pyarrow hand back) is a legitimate integer
    but is refused, because the check is isinstance(x, int)."""
    inc = bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=np.int64(inc["incident_version"]),
                 write_evidence=False)
    assert ev["status"] == "RECOVERED", ev["status"]


def test_lying_int_subclass_cannot_bypass_range_check(lake, tmp_path):
    """ADVERSARY: an int subclass whose comparisons always say True must not get an out-of-range version past
    validation into a write (defence check: must refuse or never write)."""
    class Liar(int):
        def __le__(self, o):
            return True

        def __ge__(self, o):
            return True

        def __lt__(self, o):
            return False

        def __gt__(self, o):
            return False
    bad_load(lake, M)
    from deltalake import DeltaTable
    v = DeltaTable(lake.gold).version()
    try:
        ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=Liar(999), write_evidence=False)
        status = ev["status"]
    except Exception as e:  # noqa: BLE001
        status = f"crash {e!r}"
    assert DeltaTable(lake.gold).version() == v, status
    assert status.startswith("ABORTED"), status


def test_bool_incident_version_refused(lake, tmp_path):
    """ADVERSARY: True (an int subclass) is refused (defence check)."""
    bad_load(lake, M)
    ev = recover(lake, M, tmp_path / "res", tmp_path / "bk", incident_version=True, write_evidence=False)
    assert ev["status"] == "ABORTED_INCIDENT_VERSION_INVALID"
