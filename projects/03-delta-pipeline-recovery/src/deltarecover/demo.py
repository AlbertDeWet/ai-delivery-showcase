"""One-command demo: python -m deltarecover.demo  (run from the project root with src on PYTHONPATH,
or via run_demo.py). Writes every number it reports to results/."""
from __future__ import annotations

import json
import platform
import shutil
import sys
import time
from datetime import timedelta
from pathlib import Path

from . import dedupe, dq, recon
from .generate import generate
from .pipeline import Lake, run_pipeline
from .recover import bad_load, recover, _commit_ts
from deltalake import DeltaTable

ROOT = Path(__file__).resolve().parents[2]
SEED = 20250101
INCIDENT_MONTH = "2025-07"
# Entity-months the source re-delivers (a second load). Correct silver absorbs these; the bug doubles them.
REDELIVER = {("CI-E02", "2025-03"), ("CI-E02", "2025-04"), ("CI-E03", "2025-06"), ("CI-E04", "2025-02"),
             ("CI-E05", "2025-09"), ("CI-E06", "2025-11"), ("CI-E07", "2025-05"), ("CI-E08", "2025-12"),
             ("CI-E01", "2025-08")}
DISCOVERED_AFTER = timedelta(days=5, hours=4)  # simulated: incident noticed 5d4h after the bad load


def main(work: Path | None = None) -> dict:
    work = work or ROOT / "work"
    results = ROOT / "results"
    shutil.rmtree(work, ignore_errors=True)
    results.mkdir(exist_ok=True)
    out: dict = {"seed": SEED, "python": sys.version.split()[0], "platform": platform.platform(),
                 "timings_s": {}}
    T = out["timings_s"]

    def timed(name, fn, *a, **k):
        s = time.perf_counter()
        r = fn(*a, **k)
        T[name] = round(time.perf_counter() - s, 3)
        return r

    ds = timed("generate", generate, seed=SEED)
    out["dataset"] = {"transactions": len(ds.transactions), "fx_rates": len(ds.fx),
                      "accounts": len(ds.accounts), "months": len(ds.months)}

    # ---- Incident A: duplicate bug
    lake_a = Lake(work / "incident_a")
    out["incident_a_pipeline_buggy"] = timed("pipeline_buggy", run_pipeline, lake_a, ds, REDELIVER, dedupe_bug=True)
    before = dedupe.doubled_entity_months(recon.read(lake_a.gold), recon.read(lake_a.summary))
    out["incident_a_pipeline_fixed"] = timed("pipeline_fixed", run_pipeline, lake_a, ds, REDELIVER, dedupe_bug=False)
    after = dedupe.doubled_entity_months(recon.read(lake_a.gold), recon.read(lake_a.summary))
    out["incident_a"] = {
        "redelivered_entity_months": len(REDELIVER),
        "before": {k: before[k] for k in ("entity_months_checked", "doubled_count", "duplicate_txn_ids")}
        | {"other_mismatch": len(before["other_mismatch"]), "doubled": before["doubled"]},
        "after": {k: after[k] for k in ("entity_months_checked", "doubled_count", "duplicate_txn_ids")}
        | {"other_mismatch": len(after["other_mismatch"])},
    }

    # ---- DQ suite on clean pipeline, and on a run with one missing FX rate
    out["dq_clean"] = timed("dq_clean", dq.run_suite, lake_a)
    lake_fx = Lake(work / "fx_gap")
    run_pipeline(lake_fx, ds, drop_fx={("EUR", "2025-10")})
    out["dq_missing_fx_rate"] = timed("dq_missing_fx", dq.run_suite, lake_fx)

    # ---- Incident B: bad load deletes a month, then recover
    lake_b = Lake(work / "incident_b")
    run_pipeline(lake_b, ds, REDELIVER)
    gold_before = recon.read(lake_b.gold)
    inc = bad_load(lake_b, INCIDENT_MONTH)
    inc["rows_deleted"] = gold_before.num_rows - recon.read(lake_b.gold).num_rows
    out["incident_b"] = inc
    now = _commit_ts(DeltaTable(lake_b.gold), inc["incident_version"]) + DISCOVERED_AFTER
    ev = timed("recover", recover, lake_b, INCIDENT_MONTH, results, work / "backups", now=now,
               incident_version=inc["incident_version"])
    out["recovery"] = {k: ev.get(k) for k in ("status", "restored_rows", "first_verify_pass_s", "total_s",
                                               "timings_s")}
    out["recovery"]["hours_remaining"] = ev["retention"]["hours_remaining"]
    out["recovery"]["dry_run_passed"] = sum(t["passed"] for t in ev["dry_run"])
    out["recovery"]["verify_tests"] = len(ev["dry_run"])
    out["recovery"]["cells"] = ev["reconciliation"]["cells"]
    out["recovery"]["breaks"] = ev["reconciliation"]["break_count"]
    out["recovery"]["gold_total_usd"] = ev["reconciliation"]["gold_total_usd"]
    out["recovery"]["downstream_dq_passed"] = ev["downstream_dq"]["passed"]

    # ---- Rollback proof: same incident, but a fault is injected after the restore write
    lake_r = Lake(work / "rollback_proof")
    run_pipeline(lake_r, ds, REDELIVER)
    bad_load(lake_r, INCIDENT_MONTH)
    ev_r = timed("recover_with_fault", recover, lake_r, INCIDENT_MONTH, results / "rollback_proof",
                 work / "backups_r", corrupt_restore=_double_batch, write_evidence=True)
    out["rollback_proof"] = {"status": ev_r["status"], "rollback": ev_r.get("rollback"),
                             "failed_live_tests": [t["id"] for t in ev_r["live_verify"] if not t["passed"]]}

    (results / "demo_results.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    (results / "demo_results.md").write_text(_md(out), encoding="utf-8")
    return out


def _double_batch(batch):
    """Simulated fault: the restore job writes every row twice in its own commit."""
    import pyarrow as pa
    return pa.concat_tables([batch, batch])


def _md(o: dict) -> str:
    a, r = o["incident_a"], o["recovery"]
    rows = [
        ("Source transactions generated", o["dataset"]["transactions"]),
        ("Entity-months checked (incident A)", a["before"]["entity_months_checked"]),
        ("Doubled entity-months before fix", a["before"]["doubled_count"]),
        ("Doubled entity-months after fix", a["after"]["doubled_count"]),
        ("DQ suite on clean run", "PASS" if o["dq_clean"]["passed"] else "FAIL"),
        ("DQ suite with one missing FX rate", "FAIL (detected)" if not o["dq_missing_fx_rate"]["passed"] else "PASS (missed!)"),
        ("Rows deleted by bad load (incident B)", o["incident_b"]["rows_deleted"]),
        ("Hours left before vacuum horizon (simulated clock)", r["hours_remaining"]),
        ("Dry-run verify tests passed", f"{r['dry_run_passed']}/{r['verify_tests']}"),
        ("Rows restored via time travel", r["restored_rows"]),
        ("Entity x account cells reconciled / breaks", f"{r['cells']} / {r['breaks']}"),
        ("Backup -> live verify passing (s)", r["first_verify_pass_s"]),
        ("Recovery status", r["status"]),
        ("Fault-injected run status", o["rollback_proof"]["status"]),
    ]
    L = ["# Demo results", "", "Contoso Insurance (fictional sample data). Produced by `run_demo.py`.", "",
         "| Measure | Value |", "|---|---|"] + [f"| {k} | {v} |" for k, v in rows]
    L += ["", "## Timings (s)", "", "| Step | Seconds |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in o["timings_s"].items()]
    L += ["", f"Python {o['python']} on {o['platform']}."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    res = main()
    print(json.dumps({"incident_a": res["incident_a"]["after"], "recovery": res["recovery"],
                      "rollback_proof": res["rollback_proof"]["status"]}, indent=2, default=str))
