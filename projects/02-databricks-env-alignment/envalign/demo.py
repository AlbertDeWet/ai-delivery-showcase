"""End-to-end demo: seed -> diff (before) -> plan + lint + split -> apply DEV->UAT -> apply UAT->PROD
-> diff (after) -> rollback drill. Every number printed is also written to results/.

The "approval" here is scripted so the demo can run unattended; in real use a human reads the
plan markdown and types the token (and any --accept-risk) themselves.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from .apply import apply
from dataclasses import asdict

import duckdb

from .envdiff import compare, format_matrix, matrix, protected_fingerprint, snapshot
from .normalise import norm_sql, sha
from .promote import build_plan, lint, plan_token, split, write_plan
from .seed import seed
from .workspace import PROJECT_ROOT, workspaces

PAIRS = [("dev", "uat"), ("uat", "prod"), ("dev", "prod")]


def _diffs(root) -> dict:
    ws = workspaces(root)
    snaps = {e: snapshot(w) for e, w in ws.items()}
    out = {}
    for l, r in PAIRS:
        diffs = compare(snaps[l], snaps[r])
        out[f"{l}->{r}"] = {"left": l, "right": r, "matrix": matrix(diffs), "diffs": [asdict(d) for d in diffs]}
    return out


def _view_fp(sql: str) -> str:
    """Fingerprint a view exactly as DuckDB will store it (so the drill fails on T1, not on T2)."""
    con = duckdb.connect()
    try:
        con.execute("CREATE SCHEMA core")
        con.execute(sql)
        stored = con.execute("SELECT sql FROM duckdb_views() WHERE NOT internal AND schema_name='core'").fetchone()[0]
    finally:
        con.close()
    return sha(norm_sql(stored, mask_env=False))


def _promote(root, source, target, plans_dir, log_dir, accept_r1: bool) -> list[dict]:
    plan = build_plan(root, source, target)
    findings = lint(plan, root)
    results = []
    for part in split(plan, findings):
        if not part["items"]:
            continue
        f = lint(part, root)
        pj, _ = write_plan(part, f, plans_dir)
        dry = apply(pj, target, root=root, log_dir=log_dir)
        # per-item acceptance, as a reviewer would type it after reading each finding
        accept = tuple(f"{x['rule']}:{x['item']}" for x in f
                       if accept_r1 and x["severity"] == "HIGH" and not x["blocking"])
        tok = plan_token(json.loads(pj.read_text(encoding="utf-8")))
        res = apply(pj, target, approve=tok, execute=True, accept_risks=accept, root=root, log_dir=log_dir)
        results.append({"plan": pj.name, "items": len(part["items"]), "lint": [x["rule"] for x in f],
                        "dry_run": dry["status"], "status": res["status"], "accepted": list(accept),
                        "tests_before": res.get("tests_before"), "tests_after": res.get("tests_after"),
                        "refusal": res.get("reason")})
    return results


def _rollback_drill(root: Path, results_dir: Path) -> dict:
    """Copy the aligned estate, plant a view that fails at query time, prove auto-rollback."""
    drill_root = root.parent / "workspaces_drill"
    if drill_root.exists():
        shutil.rmtree(drill_root)
    shutil.copytree(root, drill_root, ignore=shutil.ignore_patterns("_backups"))
    ws = workspaces(drill_root)["uat"]
    drill_sql = "CREATE VIEW IF NOT EXISTS core.v_drill_broken AS SELECT error('planted failure') AS x"
    plan = {"plan_id": "rollback-drill", "source": "dev", "target": "uat", "notes": [], "items": [{
        "id": "001", "kind": "view", "object": "core.v_drill_broken", "action": "create_view",
        "backup": {"type": "ddl_capture", "previous_sql": None},
        "change": [drill_sql],
        "rollback": ["DROP VIEW IF EXISTS core.v_drill_broken"], "pre_tests": [], "post_tests": [],
        "target_fingerprint": None, "expected_fingerprint": _view_fp(drill_sql)}]}
    pj, _ = write_plan(plan, lint(plan, drill_root), results_dir / "plans", drill_root)
    before = {"views": len(ws.views())}
    res = apply(pj, "uat", approve=plan_token(plan), execute=True, root=drill_root,
                log_dir=results_dir / "apply-log")
    after = {"views": len(ws.views())}
    shutil.rmtree(drill_root, ignore_errors=True)
    return {"status": res["status"], "failures": list(res.get("failures", {})),
            "restore_verified": res.get("restore_verified"), "views_before": before["views"],
            "views_after_rollback": after["views"]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="envalign demo")
    ap.add_argument("--root", default=str(PROJECT_ROOT / "workspaces"))
    ap.add_argument("--results", default=str(PROJECT_ROOT / "results"))
    a = ap.parse_args(argv)
    root, rd = Path(a.root), Path(a.results)
    t0 = time.perf_counter()
    for sub in ("plans", "apply-log"):
        shutil.rmtree(rd / sub, ignore_errors=True)
        (rd / sub).mkdir(parents=True, exist_ok=True)

    seed(root)
    ws = workspaces(root)
    prot_before = {e: protected_fingerprint(w) for e, w in ws.items()}
    before = _diffs(root)
    print("=== BEFORE ===")
    for r in before.values():
        print(format_matrix(r), "\n")

    # A first attempt WITHOUT accepting R1 shows the gate refusing the risky part.
    steps = {"dev->uat (risks not accepted)": _promote(root, "dev", "uat", rd / "plans", rd / "apply-log", False)}
    steps["dev->uat (risks accepted per item after review)"] = _promote(
        root, "dev", "uat", rd / "plans", rd / "apply-log", True)
    steps["uat->prod"] = _promote(root, "uat", "prod", rd / "plans", rd / "apply-log", True)

    after = _diffs(root)
    prot_after = {e: protected_fingerprint(w) for e, w in ws.items()}
    print("=== AFTER ===")
    for r in after.values():
        print(format_matrix(r), "\n")
    drill = _rollback_drill(root, rd)
    print("rollback drill:", drill)

    elapsed = round(time.perf_counter() - t0, 1)
    summary = {
        "demo_elapsed_seconds": elapsed,
        "company": "Contoso Insurance (fictional sample data)",
        "before": {k: v["matrix"] for k, v in before.items()},
        "after": {k: v["matrix"] for k, v in after.items()},
        "promotions": steps,
        "protected_team": {e: {"before": prot_before[e][:16], "after": prot_after[e][:16],
                               "differences": int(prot_before[e] != prot_after[e])} for e in prot_before},
        "rollback_drill": drill,
    }
    (rd / "demo-summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    for name, r in (("before", before), ("after", after)):
        for k, v in r.items():
            (rd / f"diff-{name}-{k.replace('->', '-to-')}.json").write_text(
                json.dumps(v, indent=2, default=str), encoding="utf-8")
    md = ["# Demo results", "", "Contoso Insurance (fictional sample data). Produced by `python -m envalign demo`.", "",
          "| pair | type | before identical | before aligned* | after identical | after aligned* |",
          "|---|---|---|---|---|---|"]
    for pair in before:
        for kind in ("view", "config_table", "job", "bundle"):
            b, f = before[pair]["matrix"][kind], after[pair]["matrix"][kind]
            md.append(f"| {pair} | {kind} | {b['identical']} of {b['total']} | {b['aligned']} of {b['total']} | "
                      f"{f['identical']} of {f['total']} | {f['aligned']} of {f['total']} |")
    md += ["", "*aligned = identical + expected per-environment difference (e.g. schedule paused outside PROD).", "",
           "## Promotions", ""]
    for step, rs in steps.items():
        for r in rs:
            md.append(f"- {step}: `{r['plan']}` items={r['items']} lint={r['lint']} -> **{r['status']}**"
                      f"{' (' + r['refusal'] + ')' if r['refusal'] else ''}")
    md += ["", "## Protected team (their own fingerprint query)", ""]
    for e, v in summary["protected_team"].items():
        md.append(f"- {e}: differences before vs after = {v['differences']}")
    md += ["", "## Rollback drill", "", f"- {drill}", "",
           f"Demo elapsed time (seed to summary, this machine): {elapsed} s", ""]
    (rd / "demo-summary.md").write_text("\n".join(md), encoding="utf-8")
    print(f"wrote {rd.name}/demo-summary.md")
    return demo_exit_code(steps, drill)


def demo_exit_code(steps: dict, drill: dict) -> int:
    """3 if anything ended ROLLBACK_PARTIAL, 1 if any step did not end as the story expects, else 0."""
    statuses = [r["status"] for rs in steps.values() for r in rs] + [drill.get("status")]
    if "ROLLBACK_PARTIAL" in statuses:
        return 3
    for step, rs in steps.items():
        expected_refusal = "not accepted" in step
        for r in rs:
            ok = r["status"] == "applied" or (expected_refusal and r["status"] == "refused" and r["lint"])
            if not ok:
                return 1
    return 0 if drill.get("status") == "rolled_back" and drill.get("restore_verified") else 1
