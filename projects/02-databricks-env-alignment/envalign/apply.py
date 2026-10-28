"""apply - execute an alignment plan against one environment.

Default is a DRY RUN. Executing needs all of:
  --execute, --env matching the plan target, --approve <token> equal to the hash of THIS plan,
  no blocking lint findings, explicit --accept-risk for every non-blocking HIGH finding,
  and target objects unchanged since the plan was made (stale-plan guard).
Order: full backup (+ register with SHA-256) -> baseline tests -> per-item backups -> change ->
acceptance tests -> automatic restore from the backup if anything fails.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path

from .envdiff import protected_fingerprint, snapshot
from .approvals import consume, verify
from .promote import lint_with_inputs, plan_token, target_state_fp
from .workspace import BACKUP_SCHEMA, DEFAULT_ROOT, MANAGED_SCHEMAS, PROJECT_ROOT, Workspace, workspaces

DB_KINDS = ("config_table", "view")


class ApplyRefused(Exception):
    pass


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(ws: Workspace) -> list[Path]:
    """Every file in the workspace folder - the backup is the whole estate, not a chosen subset."""
    return sorted(p for p in ws.path.rglob("*") if p.is_file())


def take_backup(ws: Workspace, plan_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = Path(ws.root) / "_backups" / ws.env / f"{stamp}-{plan_id}"
    dest.mkdir(parents=True)
    register = {"env": ws.env, "plan_id": plan_id, "taken_utc": stamp, "files": {}}
    for p in _files(ws):
        rel = p.relative_to(ws.path)
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest / rel)
        register["files"][str(rel).replace("\\", "/")] = _sha_file(p)
    for rel, h in register["files"].items():
        if _sha_file(dest / rel) != h:
            raise RuntimeError(f"backup copy of {rel} does not match source hash")
    (dest / "backup-register.json").write_text(json.dumps(register, indent=2), encoding="utf-8")
    return dest


def _make_writable(p: Path):
    try:
        os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass



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

def restore_backup(ws: Workspace, backup_dir: Path) -> tuple[bool, list[str]]:
    """Put the workspace back EXACTLY (same file set, same bytes). Never raises: every per-file problem is
    collected and returned, so the caller can report ROLLBACK_PARTIAL instead of crashing half-way."""
    errors: list[str] = []
    register = json.loads((backup_dir / "backup-register.json").read_text(encoding="utf-8"))
    for p in _files(ws):  # remove every file the change created (jobs, bundle, WAL ...)
        rel = str(p.relative_to(ws.path)).replace("\\", "/")
        if rel not in register["files"]:
            try:
                _make_writable(p)
                p.unlink()
            except OSError as e:
                errors.append(f"remove {rel}: {type(e).__name__}: {e}")
    for rel, h in register["files"].items():
        dst = ws.path / rel
        try:
            if dst.exists() and _sha_file(dst) == h:
                continue  # untouched - do not rewrite (keeps read-only files that never changed alone)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                _make_writable(dst)
            shutil.copy2(backup_dir / rel, dst)
        except OSError as e:
            errors.append(f"restore {rel}: {type(e).__name__}: {e}")
    try:
        now = {str(p.relative_to(ws.path)).replace("\\", "/"): _sha_file(p) for p in _files(ws)}
    except OSError as e:
        errors.append(f"verify: {type(e).__name__}: {e}")
        return False, errors
    if now != register["files"]:
        diff = sorted(set(now.items()) ^ set(register["files"].items()))
        errors.append(f"verify: workspace differs from backup register in {sorted({k for k, _ in diff})}")
    return not errors, errors


def run_tests(ws: Workspace, plan: dict, baseline: dict | None = None) -> dict:
    """Acceptance tests. Same tests before and after; T2 is expected to fail before the change."""
    res = {}
    con = ws.connect()
    try:
        broken = []
        for fqn in ws.views(con):
            if fqn.split(".")[0] not in MANAGED_SCHEMAS:
                continue
            try:
                con.execute(f"SELECT * FROM {fqn} LIMIT 1000").fetchall()  # COUNT(*) lets the optimiser skip columns
            except Exception as e:  # noqa: BLE001 - any failure is a test failure
                broken.append(f"{fqn}: {str(e).splitlines()[0][:120]}")
        res["T1_core_views_queryable"] = {"ok": not broken, "detail": broken}
        counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ws.tables(con) if t.split(".")[0] in MANAGED_SCHEMAS and not t.split(".")[1].startswith("cfg_")}
        objects = set(ws.views(con)) | set(ws.tables(con))
        snap = snapshot(ws, con)
    finally:
        con.close()
    mism = [f"{it['id']} {it['object']}" for it in plan["items"]
            if target_state_fp(snap, it["kind"], it["object"]) != it.get("expected_fingerprint")]
    res["T2_changed_objects_match_source"] = {"ok": not mism, "detail": mism}
    pfp = protected_fingerprint(ws, snap)
    res["T3_protected_team_untouched"] = {"ok": baseline is None or pfp == baseline["_protected_fp"],
                                          "detail": [pfp[:16]]}
    res["T4_data_tables_unchanged"] = {"ok": baseline is None or counts == baseline["_counts"], "detail": counts}
    missing = []
    for name, j in ws.jobs().items():
        for o in (j.get("tags", {}).get("uses_objects") or "").split(","):
            o = o.strip()
            if o and o.split(".")[0] in MANAGED_SCHEMAS and o not in objects:
                missing.append(f"{name} -> {o}")
    res["T5_job_dependencies_exist"] = {"ok": not missing, "detail": missing}
    res["_protected_fp"] = pfp
    res["_counts"] = counts
    return res


def _summary(t: dict) -> dict:
    return {k: v["ok"] for k, v in t.items() if not k.startswith("_")}


def _exec_db_items(ws: Workspace, items: list[dict]):
    con = ws.connect(read_only=False)
    try:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {BACKUP_SCHEMA}")
        con.execute("BEGIN TRANSACTION")
        for it in items:
            b = it.get("backup") or {}
            if b.get("type") == "backup_table":
                con.execute(f"DROP TABLE IF EXISTS {b['table']}")
                con.execute(b["sql"])
            for stmt in it["change"]:
                con.execute(stmt)
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        con.close()


def _exec_file_items(ws: Workspace, items: list[dict]):
    for it in items:
        if it["kind"] == "job":
            (ws.jobs_dir / f"{it['object']}.json").write_text(json.dumps(it["payload"], indent=2), encoding="utf-8")
        elif it["kind"] == "bundle":
            ws.bundle_path.write_text(it["payload_text"], encoding="utf-8")


def apply(plan_path, env: str, approve: str | None = None, execute: bool = False,
          accept_risks=(), root=None, log_dir: Path | None = None) -> dict:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    ws = workspaces(root)[env]
    token = plan_token(plan)
    out = {"plan_id": plan.get("plan_id"), "env": env, "mode": "execute" if execute else "dry-run",
           "items": len(plan.get("items", [])), "started_utc": datetime.now(timezone.utc).isoformat()}

    def finish(status, **kw):
        out.update(status=status, **kw)
        log_dir_ = Path(log_dir) if log_dir else PROJECT_ROOT / "results" / "apply-log"
        log_dir_.mkdir(parents=True, exist_ok=True)
        (log_dir_ / f"{plan.get('plan_id')}-{out['mode']}-{status}.json").write_text(
            json.dumps(_portable(out, PROJECT_ROOT), indent=2, default=str), encoding="utf-8")
        return out

    if plan.get("target") != env:
        return finish("refused", reason=f"plan targets {plan.get('target')!r}, not {env!r}")
    findings, inputs_hash = lint_with_inputs(plan, root)
    out["lint"] = findings
    if approve is not None and approve != token:
        return finish("refused", reason="approval token does not match this plan's content")
    problem = verify(plan, inputs_hash, root)
    if problem:
        return finish("refused", reason=f"approvals ledger: {problem}")
    blocking = [f for f in findings if f["blocking"]]
    if blocking:
        return finish("refused", reason="blocking risk findings: " + "; ".join(f["message"] for f in blocking))
    accepted = {a.strip().upper() for a in accept_risks}
    unaccepted = sorted({f"{f['rule']}:{f['item']}" for f in findings
                         if f["severity"] == "HIGH" and f"{f['rule']}:{f['item']}".upper() not in accepted})
    if unaccepted:
        return finish("refused", reason=f"HIGH risk not accepted: {unaccepted} (after review, pass "
                                        f"--accept-risk RULE:ITEM for each one)")
    stored = plan.get("approval_token")
    if stored != token:
        return finish("refused", reason="plan content no longer matches the reviewed approval token in the file")
    snap = snapshot(ws)
    stale = [it["id"] for it in plan["items"]
             if target_state_fp(snap, it["kind"], it["object"]) != it.get("target_fingerprint")]
    if stale:
        return finish("refused", reason=f"target changed since plan was made (items {stale}); re-plan")
    if not execute:
        return finish("dry-run", would_apply=[f"{i['id']} {i['kind']} {i['object']} {i['action']}"
                                              for i in plan["items"]],
                      approval_token=token)
    if approve != stored:
        return finish("refused", reason="approval token missing or does not match this plan")

    consume(plan, inputs_hash, root)  # single use: recorded before anything changes
    backup_dir = take_backup(ws, plan["plan_id"])
    out["backup_dir"] = str(backup_dir)
    before = run_tests(ws, plan)
    out["tests_before"] = _summary(before)
    db_items = [i for i in plan["items"] if i["kind"] in DB_KINDS]
    file_items = [i for i in plan["items"] if i["kind"] not in DB_KINDS]
    try:
        _exec_db_items(ws, db_items)
        _exec_file_items(ws, file_items)
        after = run_tests(ws, plan, baseline=before)
        out["tests_after"] = _summary(after)
        failed = {k: v["detail"] for k, v in after.items() if not k.startswith("_") and not v["ok"]}
    except Exception as e:  # noqa: BLE001
        failed = {"exception": [f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"]}
    if failed:
        ok, errors = restore_backup(ws, backup_dir)
        if not ok:
            # loud, explicit state: a human must finish the restore from backup_dir
            return finish("ROLLBACK_PARTIAL", failures=failed, restore_verified=False, restore_errors=errors,
                          action=f"restore manually from {backup_dir} (backup-register.json lists every file)")
        try:
            post = _summary(run_tests(ws, {"items": []}, baseline=before))
        except Exception as e:  # noqa: BLE001
            post = {"error": f"{type(e).__name__}: {e}"}
        return finish("rolled_back", failures=failed, restore_verified=True, tests_after_rollback=post)
    return finish("applied")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="envalign apply")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--approve")
    ap.add_argument("--execute", action="store_true", help="actually change the environment (default: dry run)")
    ap.add_argument("--accept-risk", action="append", default=[], help="RULE:ITEM_ID per reviewed finding, e.g. R1:005")
    ap.add_argument("--root")
    a = ap.parse_args(argv)
    res = apply(a.plan, a.env, a.approve, a.execute, tuple(a.accept_risk), a.root)
    print(json.dumps({k: v for k, v in res.items() if k != "lint"}, indent=2, default=str))
    return 0 if res["status"] in ("applied", "dry-run") else (3 if res["status"] == "ROLLBACK_PARTIAL" else 1)
