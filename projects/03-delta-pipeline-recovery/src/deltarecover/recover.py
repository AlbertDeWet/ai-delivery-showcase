"""Incident B: a bad load deleted one month of gold fact rows. Recover it with Delta time travel.

Order (each step is timed, journalled and written to the evidence pack):
  1. retention check   - time travel is lost at the EARLIER of the file-vacuum horizon
                         (deletedFileRetentionDuration) and the log-cleanup horizon (logRetentionDuration)
  2. backup            - copy of the gold table directory + sha256 manifest
  3. dry-run V01..V12  - every verify test on an in-memory candidate; nothing written yet
  4. restore           - append ONLY the pre-incident rows of the month that are not already present
                         (anti-join on normalised txn_id, so partial reloads and re-runs are safe)
  5. live verify       - the same tests against the real table, backup re-hashed
  6. rollback          - Delta RESTORE to the pre-restore version, but REFUSED if any other writer
                         committed after our restore (their data is never silently discarded)
  7. downstream checks - DQ suite

Crash safety: a journal (results/recovery_journal.json) is written at every step, and evidence is always
written when the process fails after the restore write. A re-run is idempotent: it finds nothing missing,
verifies the live table, and reports ALREADY_RECOVERED.
"""
from __future__ import annotations

import hashlib
import json
import operator
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.compute  # noqa: F401  (registers pa.compute)
from deltalake import CommitProperties, DeltaTable, write_deltalake

from . import dq, recon
from .pipeline import Lake, normalise_id
from .recon import validate_month


# ---------------------------------------------------------------- incident
def bad_load(lake: Lake, month: str) -> dict:
    validate_month(month)  # F01: never let an unvalidated value reach a predicate
    dt = DeltaTable(lake.gold)
    before = dt.version()
    res = dt.delete(f"month = '{month}'")  # safe: month matches ^\d{4}-(0[1-9]|1[0-2])$
    # incident_version is the operator handle: pass it to recover(incident_version=...)
    return {"incident_version": DeltaTable(lake.gold).version(), "pre_incident_version": before,
            "month": month, "delete_metrics": {k: v for k, v in dict(res).items() if isinstance(v, int)}}


# ---------------------------------------------------------------- helpers
def _month(t: pa.Table, month: str) -> pa.Table:
    return t.filter(pa.compute.equal(t["month"], month))


def _not_month(t: pa.Table, month: str) -> pa.Table:
    return t.filter(pa.compute.not_equal(t["month"], month))


def _ids(t: pa.Table) -> set:
    return {normalise_id(x) for x in t["txn_id"].to_pylist()}


def _commit_ts(dt: DeltaTable, version: int) -> datetime:
    for h in dt.history():
        if h.get("version") == version:
            return datetime.fromtimestamp(h["timestamp"] / 1000, tz=timezone.utc)
    raise LookupError(f"no history entry for version {version}")


_UNITS = {"nanosecond": timedelta(microseconds=0.001), "microsecond": timedelta(microseconds=1),
          "millisecond": timedelta(milliseconds=1), "second": timedelta(seconds=1),
          "minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1),
          "week": timedelta(weeks=1)}


def parse_interval(text: str) -> timedelta:
    """Delta CalendarInterval: 'interval <n> <unit>[ <n> <unit> ...]'; units nanosecond..week, singular
    or plural, any case. Months/years are rejected (not valid for these properties)."""
    s = (text or "").strip().lower()
    s = s[len("interval"):].strip() if s.startswith("interval") else s
    parts = re.findall(r"(\d+)\s*([a-z]+)", s)
    if not parts or re.sub(r"(\d+)\s*([a-z]+)", "", s).strip():
        raise ValueError(f"cannot parse retention interval {text!r}")
    total = timedelta(0)
    for n, unit in parts:
        u = unit[:-1] if unit.endswith("s") else unit
        if u not in _UNITS:
            raise ValueError(f"unsupported interval unit {unit!r} in {text!r}")
        total += int(n) * _UNITS[u]
    return total


BATCH_KEY = "deltarecover.batch_id"
ROLLBACK_KEY = "deltarecover.rollback_of"


def np_bool_types() -> tuple:
    try:
        import numpy as np
        return (np.bool_,)
    except ImportError:
        return ()


class AmbiguousIncident(LookupError):
    pass


class InvalidIncident(LookupError):
    pass


class RegistryInvalid(ValueError):
    pass


def _month_count(uri: str, version: int, month: str) -> int:
    t = DeltaTable(uri, version=version).to_pyarrow_table(columns=["month"])
    return int(pa.compute.sum(pa.compute.equal(t["month"], month)).as_py() or 0)


def incident_candidates(dt: DeltaTable, month: str) -> list[int]:
    """R2-02/R2-03: an incident is a commit at which the month's row count drops from >0 to exactly 0,
    whatever the operation (DELETE, overwrite, MERGE). Narrow deletes that leave rows are not
    incidents. Our own rollback commits (tagged in commit metadata) are excluded."""
    ours = {h["version"] for h in dt.history() if ROLLBACK_KEY in h}
    counts = [_month_count(dt.table_uri, v, month) for v in range(dt.version() + 1)]
    return [v for v in range(1, len(counts)) if counts[v - 1] > 0 and counts[v] == 0 and v not in ours]


def find_pre_incident_version(dt: DeltaTable, month: str, incident_version: int | None = None) -> int:
    """The pre-incident version is the one just before the incident commit. The operator may name the
    incident explicitly (bad_load records it); otherwise exactly one candidate must exist, else REFUSE."""
    validate_month(month)
    if incident_version is not None:  # R3-08/R3-09/R4-04/R4-05: validated, never coerced
        # R5-02/R5-03: accept any true integer type (numpy/pyarrow ints) via operator.index, then compare
        # only a plain built-in int, so an int subclass cannot override the comparisons; bool is refused.
        if isinstance(incident_version, (bool, np_bool_types())):
            raise InvalidIncident(f"incident version must be an integer, got {incident_version!r}")
        try:
            v = int.__index__(operator.index(incident_version))
        except TypeError:
            raise InvalidIncident(f"incident version must be an integer, got {incident_version!r}") from None
        assert type(v) is int
        if not 1 <= v <= dt.version():
            raise InvalidIncident(f"incident version {v} is outside the table history (1..{dt.version()})")
        before, at = _month_count(dt.table_uri, v - 1, month), _month_count(dt.table_uri, v, month)
        if not (before > 0 and at == 0):
            raise InvalidIncident(f"incident version {v} did not empty {month} "
                                  f"(rows before={before}, after={at})")
        return v - 1
    cands = incident_candidates(dt, month)
    if not cands:
        raise InvalidIncident(f"no commit removed all rows of {month}")
    if len(cands) > 1:
        raise AmbiguousIncident(f"{len(cands)} commits removed all rows of {month}: versions {cands}; "
                                "pass incident_version explicitly")
    return cands[0] - 1


def retention_window(dt: DeltaTable, incident_version: int, now: datetime) -> dict:
    """F04: time travel to the pre-incident version needs BOTH its data files (vacuum horizon,
    counted from the incident commit that removed them) and its log entries (log horizon, counted
    from the pre-incident commit). The window closes at the earlier of the two."""
    conf = dt.metadata().configuration
    file_keep = parse_interval(conf.get("delta.deletedFileRetentionDuration", "interval 7 days"))
    log_keep = parse_interval(conf.get("delta.logRetentionDuration", "interval 30 days"))
    inc_ts = _commit_ts(dt, incident_version)
    pre_ts = _commit_ts(dt, incident_version - 1) if incident_version > 0 else inc_ts
    file_h, log_h = inc_ts + file_keep, pre_ts + log_keep
    horizon = min(file_h, log_h)
    left = horizon - now
    return {"incident_commit_utc": inc_ts.isoformat(), "retention": str(file_keep),
            "log_retention": str(log_keep), "file_horizon_utc": file_h.isoformat(),
            "log_horizon_utc": log_h.isoformat(), "binding_horizon": "file" if file_h <= log_h else "log",
            "vacuum_horizon_utc": horizon.isoformat(), "now_utc": now.isoformat(),
            "hours_remaining": round(left.total_seconds() / 3600, 2), "open": left.total_seconds() > 0}


def _manifest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def backup(lake: Lake, dest_root: Path) -> dict:
    src = Path(lake.gold)
    version = DeltaTable(lake.gold).version()
    dest = dest_root / f"gold_v{version}_{datetime.now():%Y%m%d%H%M%S%f}"
    shutil.copytree(src, dest)
    m_src, m_dst = _manifest(src), _manifest(dest)
    (dest_root / f"{dest.name}.manifest.json").write_text(json.dumps(m_dst, indent=1))
    return {"backup_path": str(dest), "files": len(m_dst), "manifest_match": m_src == m_dst,
            "table_version": version, "manifest": m_dst}


def _backup_intact(b: dict) -> bool:
    return b.get("manifest_match", False) and _manifest(Path(b["backup_path"])) == b["manifest"]


# ---------------------------------------------------------------- verify tests
def verify_tests(*, candidate: pa.Table, current: pa.Table, pre: pa.Table, summary: pa.Table,
                 month: str, window: dict, pre_files_present: bool, backup_info: dict,
                 live_gold_path: str, dry_run: bool) -> list[dict]:
    restored, pre_m, cur_m = _month(candidate, month), _month(pre, month), _month(current, month)
    month_rec = recon.reconcile(restored, summary, month)
    full_rec = recon.reconcile(candidate, summary)
    control_rows = int(recon._q("SELECT sum(txn_count) FROM s", s=summary)[0][0])
    dup = recon._q("SELECT count(*) FROM (SELECT upper(trim(txn_id)) FROM c GROUP BY 1 HAVING count(*) > 1)",
                   c=candidate)[0][0]
    # V12 (F09): the backup still hashes to its manifest; in the dry run the live table must also be
    # byte-identical to the backup (nobody wrote between backup and restore).
    backup_ok = _backup_intact(backup_info)
    live_eq = _manifest(Path(live_gold_path)) == backup_info.get("manifest")
    v12 = backup_ok and (live_eq if dry_run else True)
    t = [
        ("V01", "retention window still open", window["open"], window["hours_remaining"]),
        ("V02", "pre-incident version readable and all data files present", pre_files_present, None),
        ("V03", "pre-incident version has the month; current month rows are a subset of it",
         pre_m.num_rows > 0 and _ids(cur_m) <= _ids(pre_m),
         {"pre": pre_m.num_rows, "current": cur_m.num_rows}),
        ("V04", "month after restore has exactly the pre-incident rows",
         _ids(restored) == _ids(pre_m) and restored.num_rows == pre_m.num_rows,
         {"after": restored.num_rows, "pre": pre_m.num_rows}),
        ("V05", "schema unchanged", candidate.schema.equals(pre.schema), None),
        ("V06", "no duplicate txn_id (case/whitespace normalised)", dup == 0, dup),
        ("V07", "no null keys", dq.null_keys(candidate)["passed"], None),
        ("V08", "other months untouched vs pre-incident",
         recon.cells(_not_month(candidate, month)) == recon.cells(_not_month(pre, month)), None),
        ("V09", f"restored month reconciles to the cent ({month_rec['cells']} cells)",
         month_rec["break_count"] == 0, month_rec["break_count"]),
        ("V10", f"every entity x account cell reconciles to the cent ({full_rec['cells']} cells)",
         full_rec["break_count"] == 0, {"breaks": full_rec["break_count"], "diff": full_rec["diff_total_usd"]}),
        ("V11", "row count equals control row count", candidate.num_rows == control_rows,
         {"rows": candidate.num_rows, "control": control_rows}),
        ("V12", "backup sha256 intact" + (" and live table unchanged since backup" if dry_run else ""),
         v12, {"backup_intact": backup_ok, "live_equals_backup": live_eq}),
    ]
    return [{"id": i, "name": n, "passed": bool(p), "detail": d} for i, n, p, d in t]


# ---------------------------------------------------------------- journal
class _FileLock:
    """Cross-platform exclusive lock via an O_EXCL lock file (R3-05). Stale locks (>30 s) are broken."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path, self.timeout = Path(str(path) + ".lock"), timeout

    _THREAD_LOCK = threading.Lock()  # same-process threads serialise here; the file lock covers processes

    def __enter__(self):
        self._THREAD_LOCK.acquire()
        try:
            return self._acquire_file()
        except BaseException:  # R4-01: ANY failure (timeout, unlink of a stale dir, interrupt) releases it
            self._THREAD_LOCK.release()
            raise

    def _acquire_file(self):
        start = time.monotonic()
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except (FileExistsError, PermissionError):  # Windows: a lock file pending delete -> PermissionError
                try:
                    if time.time() - self.path.stat().st_mtime > self.timeout:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() - start > self.timeout:
                    raise TimeoutError(f"could not lock {self.path}")
                time.sleep(0.01)

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
            self.path.unlink(missing_ok=True)
        finally:
            self._THREAD_LOCK.release()


def _append_json_array(path: Path, entry: dict) -> None:
    """O(1) append to a file that always stays a valid JSON array (R3-11), under a lock (R3-05)."""
    line = json.dumps(entry, default=str).encode()
    with _FileLock(path):
        if not path.exists() or path.stat().st_size == 0:
            path.write_bytes(b"[\n" + line + b"\n]\n")
            return
        with path.open("r+b") as f:
            if f.read(64).lstrip()[:1] != b"[":
                raise RegistryInvalid(f"{path} is not a JSON array; refusing to append")
            # R5-01: walk back over ANY amount of trailing whitespace to the closing bracket
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            while pos > 0:
                step = min(4096, pos)
                f.seek(pos - step)
                chunk = f.read(step)
                stripped = chunk.rstrip()
                if stripped:
                    pos = pos - step + len(stripped) - 1
                    break
                pos -= step
            f.seek(pos)
            if f.read(1) != b"]":
                raise RegistryInvalid(f"{path} does not end with ']'; refusing to append")
            f.seek(max(0, pos - 4096))
            empty = f.read(pos - max(0, pos - 4096)).rstrip().endswith(b"[")
            f.seek(pos)
            f.truncate()
            f.write((b"" if empty else b",\n") + line + b"\n]\n")


class Journal:
    """Append-only across runs and safe for concurrent runs (R2-09, R3-05, R3-11): every step is one locked
    O(1) append; nothing is held in memory and rewritten. An unreadable journal is moved aside, never lost."""

    def __init__(self, results_dir: Path, ev: dict):
        self.path, self.ev = Path(results_dir) / "recovery_journal.json", ev
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                if not isinstance(json.loads(self.path.read_text()), list):
                    raise ValueError("not a list")
            except ValueError:
                self.path.rename(self.path.with_name(
                    f"recovery_journal.unreadable-{datetime.now():%Y%m%d%H%M%S%f}.json"))

    def step(self, name: str, **info) -> None:
        entry = {"run_id": self.ev["run_id"], "step": name,
                 "at_utc": datetime.now(timezone.utc).isoformat(), **info}
        self.ev.setdefault("journal", []).append(entry)
        _append_json_array(self.path, entry)


# ---------------------------------------------------------------- batch registry (R3-01)
def _registry(lake: Lake) -> Path:
    """The tool's own record of restore batches it intended to write. It lives beside the tables (not in a
    results dir), so any later run on the same lake can recognise our batches; a commit-metadata tag alone
    can be forged by any writer and is never trusted on its own."""
    p = Path(lake.root) / "_deltarecover" / "batch_registry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _register_batch(lake: Lake, batch_id: str, month: str, run_id: str, rows: int) -> None:
    _append_json_array(_registry(lake), {"batch_id": batch_id, "month": month, "run_id": run_id, "rows": rows,
                                         "at_utc": datetime.now(timezone.utc).isoformat()})


def registry_problem(lake: Lake) -> str | None:
    """R4-02: None when the registry is absent or a valid JSON array of objects, else a description."""
    p = _registry(lake)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        return f"{p} is not valid JSON: {e}"
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        return f"{p} is valid JSON but not an array of objects"
    return None


def _registered(lake: Lake, batch_id: str, month: str) -> bool:
    p = _registry(lake)
    if not p.exists():
        return False
    data = json.loads(p.read_text(encoding="utf-8"))  # validity is checked up front by recover()
    return any(e.get("batch_id") == batch_id and e.get("month") == month for e in data)


def _commits_after(dt: DeltaTable, version: int) -> list[dict]:
    return sorted((h for h in dt.history() if h.get("version", -1) > version), key=lambda h: h["version"])


def _rollback_to(lake: Lake, version: int, reason: str) -> int:
    DeltaTable(lake.gold).restore(version, commit_properties=CommitProperties(
        custom_metadata={ROLLBACK_KEY: str(version), "deltarecover.reason": reason}))
    return DeltaTable(lake.gold).version()


def _touches_only(lake: Lake, version: int, month: str) -> bool:
    """True when commit `version` changed no month other than `month` (rows and amounts of every other
    month identical before and after the commit)."""
    a, b = recon.read(lake.gold, version - 1), recon.read(lake.gold, version)
    return recon.cells(_not_month(a, month)) == recon.cells(_not_month(b, month)) and \
        _not_month(a, month).num_rows == _not_month(b, month).num_rows


def _cleanup_unverified_batch(lake: Lake, month: str, pre: pa.Table, incident_v: int, summary: pa.Table,
                              jr: "Journal") -> dict | None:
    """R2-06 / R3-01: if the month is inconsistent with the pre-incident rows, roll back the batch that caused it
    ONLY when (a) its batch id is in our own registry for this month, (b) the commit touched only this month,
    and (c) no other commit follows it. Every other inconsistent case is refused with a specific status."""
    cur = recon.read(lake.gold)
    cur_m, pre_m = _month(cur, month), _month(pre, month)
    pre_amt = {normalise_id(i): a for i, a in zip(pre_m["txn_id"].to_pylist(), pre_m["amount_usd"].to_pylist())}
    cur_pairs = list(zip(cur_m["txn_id"].to_pylist(), cur_m["amount_usd"].to_pylist()))
    dup = len(cur_pairs) != len(_ids(cur_m))
    if not dup and all(pre_amt.get(normalise_id(i)) == a for i, a in cur_pairs):
        return None  # consistent (complete or partial): keep it, the restore tops up
    after = _commits_after(DeltaTable(lake.gold), incident_v)
    batches = [h for h in after if BATCH_KEY in h]
    if not batches:
        info = {"action": "refused", "status": "ABORTED_INCONSISTENT_MONTH_UNTAGGED",
                "reason": f"{month} holds rows that differ from the pre-incident version (duplicates={dup}) and no "
                          "recover batch wrote them; a person must inspect commits "
                          f"{[h['version'] for h in after]}"}
        jr.step("resume_cleanup_refused", **info)
        return info
    last = batches[-1]
    info = {"batch_id": last[BATCH_KEY], "batch_version": last["version"]}
    if not _registered(lake, last[BATCH_KEY], month):
        info.update(action="refused", status="ABORTED_UNRECOGNISED_BATCH",
                    reason="batch id is not in this tool's registry (forged or foreign tag)")
    elif not _touches_only(lake, last["version"], month):
        info.update(action="refused", status="ABORTED_BATCH_TOUCHES_OTHER_MONTHS",
                    reason=f"commit {last['version']} changed months other than {month}")
    elif any(h["version"] > last["version"] for h in after):
        info.update(action="refused", status="ABORTED_UNVERIFIED_BATCH_FOLLOWED_BY_OTHER_COMMITS",
                    reason="other commits follow the unverified batch")
    if info.get("action") == "refused":
        jr.step("resume_cleanup_refused", **info)
        return info
    info["new_version"] = _rollback_to(lake, last["version"] - 1, f"unverified batch {last[BATCH_KEY]}")
    info["action"] = "rolled_back"
    jr.step("resume_cleanup_rolled_back", **info)
    return info


def _later_commit_explanation(lake: Lake, pre: pa.Table, current: pa.Table, month: str, incident_v: int) -> dict:
    """R2-11: name the later commit(s) and rows that make other months differ from the pre-incident version."""
    pre_o, cur_o = _not_month(pre, month), _not_month(current, month)
    pre_ids = _ids(pre_o)
    new_rows = [(i, m) for i, m in zip(cur_o["txn_id"].to_pylist(), cur_o["month"].to_pylist())
                if normalise_id(i) not in pre_ids]
    pre_em, cur_em = recon.entity_month(pre_o), recon.entity_month(cur_o)
    months = sorted({m for _, m in new_rows} |
                    {k[1] for k in set(pre_em) | set(cur_em) if pre_em.get(k) != cur_em.get(k)})
    later = [h["version"] for h in _commits_after(DeltaTable(lake.gold), incident_v)
             if BATCH_KEY not in h and ROLLBACK_KEY not in h]
    return {"later_commits": later, "months": months, "new_txn_ids": [i for i, _ in new_rows][:20]}


# ---------------------------------------------------------------- main entry point
def recover(lake: Lake, month: str, results_dir: Path, backup_root: Path, now: datetime | None = None,
            inject_fault: Callable[[Lake], None] | None = None,
            corrupt_restore: Callable[[pa.Table], pa.Table] | None = None,
            write_evidence: bool = True, incident_version: int | None = None) -> dict:
    """`incident_version`: the bad load's commit (bad_load returns it). If omitted, exactly one commit may
    have emptied the month, otherwise recover REFUSES as ambiguous.
    `inject_fault` runs after the restore commit (simulates crashes / concurrent writers);
    `corrupt_restore` alters the batch before it is written (simulates a bad restore job)."""
    validate_month(month)
    results_dir = Path(results_dir)
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    ev: dict = {"run_id": f"{datetime.now():%Y%m%d%H%M%S%f}-{uuid.uuid4().hex[:6]}", "month": month,
                "timings_s": timings}
    jr = Journal(results_dir, ev)

    def lap(name: str, start: float) -> float:
        timings[name] = round(time.perf_counter() - start, 3)
        jr.step(name)
        return time.perf_counter()

    s = time.perf_counter()
    dt = DeltaTable(lake.gold)
    try:
        pre_v = find_pre_incident_version(dt, month, incident_version)
    except AmbiguousIncident as e:
        ev.update(status="ABORTED_AMBIGUOUS_INCIDENT", error=str(e))
        return _finish(ev, results_dir, write_evidence, t0)
    except OSError as e:  # R3-06: reading an old version hit vacuumed data files
        ev.update(status="ABORTED_PRE_INCIDENT_FILES_MISSING", error=repr(e)[:500],
                  reason="data files of a pre-incident version are gone (vacuumed?); time travel cannot restore")
        return _finish(ev, results_dir, write_evidence, t0)
    except LookupError as e:  # InvalidIncident and anything else that cannot name the incident
        ev.update(status="ABORTED_INCIDENT_VERSION_INVALID", error=str(e), incident_version_supplied=repr(incident_version))
        return _finish(ev, results_dir, write_evidence, t0)
    incident_v = pre_v + 1
    now = now or datetime.now(timezone.utc)
    ev.update({"current_version": dt.version(), "pre_incident_version": pre_v, "incident_version": incident_v,
               "incident_source": "operator" if incident_version is not None else "history"})
    try:
        window = retention_window(dt, incident_v, now)
    except ValueError as e:  # F12: unparseable property -> refuse with evidence, never crash
        ev.update(status="ABORTED_RETENTION_UNPARSEABLE", error=str(e))
        return _finish(ev, results_dir, write_evidence, t0)
    ev["retention"] = window
    problem = registry_problem(lake)
    if problem:  # R4-02: an unreadable registry means batches cannot be recognised -> refuse, change nothing
        ev.update(status="ABORTED_REGISTRY_INVALID", reason=problem)
        return _finish(ev, results_dir, write_evidence, t0)
    s = lap("1_retention_check", s)
    if not window["open"]:
        ev["status"] = "ABORTED_RETENTION_EXPIRED"
        return _finish(ev, results_dir, write_evidence, t0)

    # R3-06: files of the pre-incident version may have been vacuumed (e.g. retention check disabled):
    # the property-based window cannot see that, so check the files and refuse with evidence.
    try:
        pre_dt = DeltaTable(lake.gold, version=pre_v)
        missing_files = [u for u in pre_dt.file_uris()
                         if not Path(u.replace("file:///", "").replace("file://", "")).exists()]
        pre = pre_dt.to_pyarrow_table() if not missing_files else None
    except Exception as e:  # noqa: BLE001 - unreadable pre-incident version
        missing_files, pre, ev["error"] = ["<unreadable>"], None, repr(e)
    pre_files_present = not missing_files
    if not pre_files_present:
        ev.update(status="ABORTED_PRE_INCIDENT_FILES_MISSING", missing_files=missing_files[:20],
                  reason="pre-incident data files are gone (vacuumed?); time travel cannot restore the month")
        return _finish(ev, results_dir, write_evidence, t0)
    summary = recon.read(lake.summary)

    t_backup = time.perf_counter()
    b = backup(lake, backup_root)  # R3-02: backup BEFORE any change, including resume clean-up
    ev["backup"] = {k: v for k, v in b.items() if k != "manifest"}
    s = lap("2_backup", s)

    cleanup = _cleanup_unverified_batch(lake, month, pre, incident_v, summary, jr)
    if cleanup:
        ev["resume_cleanup"] = cleanup
        if cleanup["action"] == "refused":
            ev.update(status=cleanup["status"], reason=cleanup["reason"])
            return _finish(ev, results_dir, write_evidence, t0)
        b = backup(lake, backup_root)  # the table changed: fresh backup for V12 (both are kept)
        ev["backup_after_cleanup"] = {k: v for k, v in b.items() if k != "manifest"}

    current = recon.read(lake.gold)
    present = _ids(_month(current, month))
    pre_m = _month(pre, month)
    missing = pre_m.filter(pa.array([normalise_id(x) not in present for x in pre_m["txn_id"].to_pylist()],
                                    type=pa.bool_()))
    missing = missing.select(current.schema.names).cast(current.schema)
    ev["already_present_rows"] = len(present)
    candidate = pa.concat_tables([current, missing])
    common = dict(pre=pre, summary=summary, month=month, window=window, pre_files_present=pre_files_present,
                  backup_info=b, live_gold_path=lake.gold)
    dry = verify_tests(candidate=candidate, current=current, dry_run=True, **common)
    ev["dry_run"] = dry
    s = lap("3_dry_run_verify", s)
    if not all(t["passed"] for t in dry):
        failed = {t["id"] for t in dry if not t["passed"]}
        if "V08" in failed:  # R2-11: other months changed after the incident; name the commit(s) and rows
            why = _later_commit_explanation(lake, pre, current, month, incident_v)
            ev.update(status="ABORTED_UNRECONCILED_LATER_COMMIT", later_commit=why,
                      reason=(f"months {why['months']} changed after the incident in commits {why['later_commits']} "
                              f"(new rows {why['new_txn_ids']}); they are not in the independent control, so "
                              "recover cannot reconcile to the cent. Refresh the control for them, then re-run."))
        else:
            ev["status"] = "ABORTED_DRY_RUN_FAILED"
        ev["failed_tests"] = sorted(failed)
        return _finish(ev, results_dir, write_evidence, t0)

    if missing.num_rows == 0:  # idempotent re-run (e.g. after a crash): nothing to write
        ev.update(restored_rows=0, reconciliation=recon.reconcile(current, summary),
                  downstream_dq=dq.run_suite(lake), first_verify_pass_s=round(time.perf_counter() - t_backup, 3))
        ev["status"] = "ALREADY_RECOVERED" if ev["downstream_dq"]["passed"] else "ALREADY_RECOVERED_DQ_WARNINGS"
        lap("7_downstream_checks", s)
        return _finish(ev, results_dir, write_evidence, t0)

    version_before_restore = DeltaTable(lake.gold).version()
    batch = corrupt_restore(missing) if corrupt_restore else missing
    batch_id = uuid.uuid4().hex
    ev.update(batch_id=batch_id, restored_rows=batch.num_rows, version_before_restore=version_before_restore)
    # R2-07: intent is journalled BEFORE the write; everything from the write onward is inside one try.
    _register_batch(lake, batch_id, month, ev["run_id"], batch.num_rows)
    jr.step("4_restore_intent", batch_id=batch_id, rows=batch.num_rows, version_before=version_before_restore)
    try:
        write_deltalake(lake.gold, batch, mode="append", partition_by=["month"],
                        commit_properties=CommitProperties(custom_metadata={BATCH_KEY: batch_id}))
        our_version = next(h["version"] for h in DeltaTable(lake.gold).history() if h.get(BATCH_KEY) == batch_id)
        ev["version_after_restore"] = our_version
        jr.step("4_restore_committed", batch_id=batch_id, version=our_version)
        if inject_fault:
            inject_fault(lake)
        s = lap("4_restore", s)
        live = recon.read(lake.gold)
        live_tests = verify_tests(candidate=live, current=current, dry_run=False, **common)
        ev["live_verify"] = live_tests
        ev["first_verify_pass_s"] = round(time.perf_counter() - t_backup, 3)
        s = lap("5_live_verify", s)
        ev["reconciliation"] = recon.reconcile(live, summary)
    except BaseException as e:  # F03 / R2-07: never leave a silent unverified write
        committed = any(h.get(BATCH_KEY) == batch_id for h in DeltaTable(lake.gold).history())
        ev.update(status="CRASHED_AFTER_RESTORE_UNVERIFIED" if committed else "CRASHED_BEFORE_COMMIT",
                  error=repr(e), batch_committed=committed,
                  resume="re-run recover: it verifies the batch, rolls it back if inconsistent, then retries")
        jr.step("crashed", error=repr(e), batch_id=batch_id, committed=committed)
        _finish(ev, results_dir, True, t0)
        raise

    if not all(t["passed"] for t in live_tests):
        ev["first_verify_pass_s"] = None
        foreign = [h["version"] for h in _commits_after(DeltaTable(lake.gold), our_version)]
        if foreign:  # F02: someone else committed after us; RESTORE would discard their data
            ev["rollback"] = {"refused": True, "foreign_commits_after_restore": foreign,
                              "reason": "other writers committed after the restore; manual merge required"}
            ev["status"] = "ROLLBACK_REFUSED_CONCURRENT_WRITES"
        else:
            jr.step("6_rollback_intent", to_version=version_before_restore)
            try:
                new_v = _rollback_to(lake, version_before_restore, f"live verify failed for batch {batch_id}")
            except BaseException as e:  # R2-08: a failed rollback still leaves evidence
                ev.update(status="ROLLBACK_FAILED", error=repr(e),
                          rollback={"attempted_to_version": version_before_restore, "failed": True})
                jr.step("rollback_failed", error=repr(e))
                _finish(ev, results_dir, True, t0)
                raise
            after = recon.read(lake.gold)
            ev["rollback"] = {"restored_to_version": version_before_restore, "new_version": new_v,
                              "rows_after_rollback": after.num_rows,
                              "matches_pre_restore_state": recon.cells(after) == recon.cells(current)}
            ev["status"] = "ROLLED_BACK"
        lap("6_rollback", s)
        return _finish(ev, results_dir, write_evidence, t0)

    ev["downstream_dq"] = dq.run_suite(lake)
    lap("7_downstream_checks", s)
    ev["status"] = "RECOVERED" if ev["downstream_dq"]["passed"] else "RECOVERED_DQ_WARNINGS"
    return _finish(ev, results_dir, write_evidence, t0)



def _portable(obj, root):
    """Copy of obj with absolute paths under root written relative to it (forward slashes), so
    recorded evidence never carries a machine-specific folder."""
    import os as _os
    root_s = _os.path.normcase(str(root))
    if isinstance(obj, dict):
        return {k: _portable(v, root) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_portable(v, root) for v in obj]
    if isinstance(obj, (str, Path)):
        s = str(obj)
        n = _os.path.normcase(s)
        if n == root_s:
            return "."
        if n.startswith(root_s + _os.sep) or n.startswith(root_s + "/"):
            return s[len(root_s) + 1:].replace("\\", "/")
        return s if isinstance(obj, str) else obj
    return obj

def _finish(ev: dict, results_dir: Path, write: bool, t0: float) -> dict:
    """Writes the latest evidence AND a per-run copy under runs/ (R2-09: earlier runs are never lost)."""
    ev["total_s"] = round(time.perf_counter() - t0, 3)
    if write:
        runs = results_dir / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        pev = _portable(ev, Path(__file__).resolve().parents[2])
        body, md = json.dumps(pev, indent=2, default=str), evidence_markdown(pev)
        (runs / f"recovery_evidence_{ev['run_id']}.json").write_text(body, encoding="utf-8")
        (results_dir / "recovery_evidence.json").write_text(body, encoding="utf-8")
        (results_dir / "recovery_evidence.md").write_text(md, encoding="utf-8")
    return ev


def evidence_markdown(ev: dict) -> str:
    L = [f"# Recovery evidence pack - month {ev['month']}", "",
         "Contoso Insurance (fictional sample data). Generated by `deltarecover.recover`.", "",
         f"**Status: {ev.get('status')}**", "", "| Item | Value |", "|---|---|",
         f"| Pre-incident version | {ev.get('pre_incident_version')} |",
         f"| Incident version | {ev.get('incident_version')} |"]
    if "retention" in ev:
        r = ev["retention"]
        L += [f"| Vacuum horizon (UTC, binding: {r['binding_horizon']}) | {r['vacuum_horizon_utc']} |",
              f"| Hours remaining at start | {r['hours_remaining']} |"]
    if "error" in ev:
        L.append(f"| Error | {ev['error']} |")
    if "backup" in ev:
        L.append(f"| Backup files / manifest match | {ev['backup']['files']} / {ev['backup']['manifest_match']} |")
    if "restored_rows" in ev:
        L.append(f"| Rows restored (already present) | {ev['restored_rows']} ({ev.get('already_present_rows', 0)}) |")
    if ev.get("first_verify_pass_s") is not None:
        L.append(f"| Backup -> live verify passing (s) | {ev['first_verify_pass_s']} |")
    if "reconciliation" in ev:
        r = ev["reconciliation"]
        L.append(f"| Cells reconciled / breaks | {r['cells']} / {r['break_count']} |")
        L.append(f"| Gold total USD / control total USD | {r['gold_total_usd']} / {r['summary_total_usd']} |")
    L.append(f"| Total runtime (s) | {ev['total_s']} |")
    for key, title in (("dry_run", "Dry-run verify"), ("live_verify", "Live verify")):
        if key in ev:
            L += ["", f"## {title}", "", "| Test | Name | Result |", "|---|---|---|"]
            L += [f"| {t['id']} | {t['name']} | {'PASS' if t['passed'] else 'FAIL'} |" for t in ev[key]]
    if "rollback" in ev:
        L += ["", "## Rollback", "", "```json", json.dumps(ev["rollback"], indent=2), "```"]
    if "downstream_dq" in ev:
        L += ["", "## Downstream DQ", "", "| Check | Result |", "|---|---|"]
        L += [f"| {c['check']} | {'PASS' if c['passed'] else 'FAIL'} |" for c in ev["downstream_dq"]["checks"]]
    L += ["", "## Step timings (s)", "", "| Step | Seconds |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in ev["timings_s"].items()]
    return "\n".join(L) + "\n"
