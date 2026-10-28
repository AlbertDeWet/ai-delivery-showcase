"""promote - build a PR-sized alignment plan (source env -> target env) and lint it for risk.

Every plan item carries: backup step, change, pre-tests, post-tests, rollback step, risks.
The approval token is a hash of the plan content, so approving one plan never approves another.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .envdiff import compare, fingerprint, snapshot
from .normalise import EXPECTED_JOB_FIELDS, env_substitute, row_hash, sha, strip_sql_comments, view_body
from .sqlguard import check_many
from .workspace import BACKUP_SCHEMA, PROTECTED_SCHEMAS, Workspace, is_protected, workspaces

ACCEPTABLE_RULES = {"R1", "R5"}  # accepted per item (RULE:ITEM_ID) by a human; R2/R3 never


def q(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def _ident(fqn: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", fqn):
        raise ValueError(f"unsafe identifier: {fqn!r}")
    return fqn


def _view_items(diffs, src, tgt):
    items = []
    for d in diffs:
        if d.kind != "view" or d.protected or d.status not in ("drift", "missing"):
            continue
        name = _ident(d.obj)
        body = env_substitute(view_body(src["view"][d.obj]["sql"]), src["env"], tgt["env"])
        prev = tgt["view"].get(d.obj, {}).get("sql")
        if d.status == "missing":
            change = [f"CREATE VIEW IF NOT EXISTS {name} AS {body}"]
            rollback = [f"DROP VIEW IF EXISTS {name}"]
            action = "create_view"
        else:
            change = [f"CREATE OR REPLACE VIEW {name} AS {body}"]
            rollback = [f"CREATE OR REPLACE VIEW {name} AS {view_body(prev)}"]
            action = "replace_view"
        items.append({
            "kind": "view", "object": d.obj, "action": action, "reason": d.detail,
            "backup": {"type": "ddl_capture", "object": d.obj, "previous_sql": prev,
                       "note": "previous definition stored in the plan and in the full catalog copy"},
            "change": change, "rollback": rollback,
            "pre_tests": ["fingerprint matches plan (stale-plan guard)"] + (["view is queryable"] if prev else []),
            "post_tests": ["view is queryable", "normalised DDL == source DDL", "all core views queryable"],
        })
    # dependency order: a view that references another planned view goes after it
    names = {it["object"]: it for it in items}
    ordered, seen = [], set()

    def visit(n, stack=()):
        if n in seen or n in stack:
            return
        for other in names:
            if other != n and re.search(rf"\b{re.escape(other)}\b", " ".join(names[n]["change"])):
                visit(other, stack + (n,))
        seen.add(n)
        ordered.append(names[n])
    for n in names:
        visit(n)
    return ordered


def _cfg_items(diffs, src, tgt, plan_tag):
    items = []
    s_cfg, t_cfg = src["_cfg_raw"], tgt["_cfg_raw"]
    for d in diffs:
        if d.kind != "config_table" or d.protected or d.status not in ("drift", "missing"):
            continue
        name = _ident(d.obj)
        s = s_cfg[d.obj]
        t = t_cfg.get(d.obj)
        bak = f"{BACKUP_SCHEMA}.{name.split('.')[1]}__{plan_tag}"
        cols = ", ".join(f"{c} {ty}" for c, ty in s["columns"])
        colnames = [c for c, _ in s["columns"]]

        def ins(r):
            return f"INSERT INTO {name} VALUES ({', '.join(q(r[c]) for c in colnames)})"
        if t is None or [list(c) for c in t["columns"]] != [list(c) for c in s["columns"]]:
            action = "create_table" if t is None else "rebuild_table"
            change = ([] if t is None else [f"DROP TABLE {name}"]) + [f"CREATE TABLE {name}({cols})"] + \
                     [ins(r) for r in s["rows"]]
        else:
            action = "upsert_rows"
            sk = {str(r["key"]): r for r in s["rows"]}
            tk = {str(r["key"]): r for r in t["rows"]}
            change = []
            removed = sorted(set(tk) - set(sk))
            for k in removed:
                change.append(f"DELETE FROM {name} WHERE key = {q(k)}")
            for k in sorted(sk):
                if k not in tk or row_hash({c: sk[k].get(c) for c in colnames}) !=                         row_hash({c: tk[k].get(c) for c in colnames}):
                    if k in tk:
                        change.append(f"DELETE FROM {name} WHERE key = {q(k)}")
                    change.append(ins(sk[k]))
        removed = removed if action == "upsert_rows" else []
        if t is None:
            backup = {"type": "none_needed", "note": "table does not exist in target; full catalog copy still taken"}
            rollback = [f"DROP TABLE IF EXISTS {name}"]
        else:
            backup = {"type": "backup_table", "table": bak, "sql": f"CREATE TABLE {bak} AS SELECT * FROM {name}"}
            rollback = [f"DROP TABLE IF EXISTS {name}", f"CREATE TABLE {name} AS SELECT * FROM {bak}"]
        items.append({
            "kind": "config_table", "object": d.obj, "action": action, "reason": d.detail,
            "backup": backup, "change": change, "rollback": rollback, "removes_keys": removed,
            "pre_tests": ["fingerprint matches plan (stale-plan guard)", "row count recorded"],
            "post_tests": ["row hashes per key == source", "schema == source", "all core views queryable"],
        })
    return items


def _job_payload(src_job: dict, tgt_job: dict | None, src_env: str, dst_env: str) -> dict:
    j = json.loads(env_substitute(json.dumps(src_job), src_env, dst_env))
    if tgt_job:  # keep the fields that are allowed to differ per environment
        for f in EXPECTED_JOB_FIELDS:
            parts = f.split(".")
            if "*" in parts:
                if parts[0] == "job_clusters":
                    tw = {c["job_cluster_key"]: c for c in tgt_job.get("job_clusters", [])}
                    for c in j.get("job_clusters", []):
                        if c["job_cluster_key"] in tw:
                            c["new_cluster"]["num_workers"] = tw[c["job_cluster_key"]]["new_cluster"].get("num_workers")
                continue
            sv, tv = j, tgt_job
            for p in parts[:-1]:
                sv, tv = sv.get(p, {}), tv.get(p, {})
            if parts[-1] in tv:
                sv[parts[-1]] = tv[parts[-1]]
    return j


def _job_items(diffs, src, tgt, src_env, dst_env):
    items = []
    for d in diffs:
        if d.kind != "job" or d.protected or d.status not in ("drift", "missing"):
            continue
        tgt_job = tgt["job"].get(d.obj, {}).get("raw")
        payload = _job_payload(src["job"][d.obj]["raw"], tgt_job, src_env, dst_env)
        items.append({
            "kind": "job", "object": d.obj, "action": "deploy_job", "reason": d.detail,
            "backup": {"type": "file_copy", "path": f"jobs/{d.obj}.json", "previous": tgt_job},
            "change": [f"write jobs/{d.obj}.json (source definition, env tokens -> {dst_env}, env fields kept)"],
            "payload": payload,
            "rollback": [f"restore jobs/{d.obj}.json from backup" if tgt_job else f"delete jobs/{d.obj}.json"],
            "pre_tests": ["fingerprint matches plan (stale-plan guard)"],
            "post_tests": ["job JSON == source after env masking", "every uses_objects entry exists in target"],
        })
    return items


def _bundle_items(diffs, src_ws):
    bd = [d for d in diffs if d.kind == "bundle" and d.status in ("drift", "missing", "extra")]
    if not bd:
        return []
    return [{
        "kind": "bundle", "object": "databricks.yml", "action": "deploy_bundle",
        "reason": [f"{d.obj}: {d.status} {d.detail}" for d in bd],
        "backup": {"type": "file_copy", "path": "databricks.yml"},
        "change": ["write databricks.yml = source bundle (one bundle, all targets)"],
        "payload_text": src_ws.bundle_path.read_text(encoding="utf-8"),
        "rollback": ["restore databricks.yml from backup"],
        "pre_tests": ["fingerprint matches plan (stale-plan guard)"],
        "post_tests": ["bundle sections == source"],
    }]


def target_state_fp(snap: dict, kind: str, obj: str) -> str | None:
    """Fingerprint used to prove an item landed: env-masked for jobs, whole-file sections for the bundle."""
    if kind == "bundle":
        return sha({k: v["hash"] for k, v in snap["bundle"].items()})
    if kind == "job":
        o = snap["job"].get(obj)
        return None if o is None else sha(o["norm_env"])
    return fingerprint(snap, kind, obj)


def plan_hash(plan: dict) -> str:
    """Deterministic content hash: the creation time and the review artefacts are not content."""
    return sha({k: v for k, v in plan.items() if k not in ("approval_token", "lint", "created_utc")})


def plan_token(plan: dict) -> str:
    """Human-typed identifier of a plan. NOT proof of approval on its own: apply also checks the HMAC ledger."""
    return "APPROVE-" + plan_hash(plan)[:16]


def masked_fp(snap: dict, kind: str, obj: str) -> str | None:
    """Env-masked fingerprint: two envs holding the same version (each with its own env token) compare equal."""
    o = snap[kind].get(obj)
    if o is None:
        return None
    if kind == "view":
        return sha(o["norm_env"])
    return sha({"c": o["columns"], "r": {k: v["h_env"] for k, v in o["rows"].items()}})


def build_plan(root=None, source="dev", target="uat", kinds=("config_table", "view", "job", "bundle")) -> dict:
    ws = workspaces(root)
    s_ws, t_ws = ws[source], ws[target]
    src, tgt = snapshot(s_ws), snapshot(t_ws)
    others_snaps = [src] + [snapshot(w) for e, w in ws.items() if e not in (source, target) and w.db_path.exists()]
    diffs = compare(src, tgt)
    tag = sha([source, target, sorted((d.kind, d.obj, d.status) for d in diffs)])[:8]
    items = []
    if "config_table" in kinds:
        items += _cfg_items(diffs, src, tgt, tag)
    if "view" in kinds:
        items += _view_items(diffs, src, tgt)
    if "job" in kinds:
        items += _job_items(diffs, src, tgt, source, target)
    if "bundle" in kinds:
        items += _bundle_items(diffs, s_ws)
    for i, it in enumerate(items, 1):
        it["id"] = f"{i:03d}"
        it["target_fingerprint"] = target_state_fp(tgt, it["kind"], it["object"])
        it["expected_fingerprint"] = target_state_fp(src, it["kind"], it["object"])
        # PROD-only (target-only) change: the target's version exists in no other environment
        if it["kind"] in ("view", "config_table") and it["target_fingerprint"] is not None:
            mine = masked_fp(tgt, it["kind"], it["object"])
            others = [masked_fp(o, it["kind"], it["object"]) for o in others_snaps]
            if all(fp != mine for fp in others):
                it["target_only_change"] = True
    notes = [f"{d.kind} {d.obj}: only in {target} - report only, not dropped (needs owner sign-off)"
             for d in diffs if d.status == "extra" and not d.protected and d.kind != "bundle"]
    notes += [f"protected {d.kind} {d.obj}: {d.status} - excluded by design" for d in diffs
              if d.protected and d.status != "identical"]
    plan = {"plan_id": f"{source}-to-{target}-{tag}", "source": source, "target": target,
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "items": items, "notes": notes}
    return plan


# ---------------------------------------------------------------------------------------------
# Risk linter
# ---------------------------------------------------------------------------------------------
_QUOTED = re.compile(r'"((?:[^"]|"")*)"|`((?:[^`]|``)*)`|\[([^\]]*)\]')
_REF = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.)?([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b")
_REPLACES = re.compile(r"create\s+or\s+replace|drop\s+(table|view)", re.I)


def _canon(sql: str) -> str:
    """Comments removed, quoted/backticked/bracketed identifiers unquoted, everything lower-case."""
    s = strip_sql_comments(sql or "")
    s = _QUOTED.sub(lambda m: next(g for g in m.groups() if g is not None), s)
    return s.lower()


def sql_refs(sql: str) -> set[str]:
    return {f"{a}.{b}" for a, b in _REF.findall(_canon(sql))}


def touches_protected(text: str) -> bool:
    c = _canon(text)
    return any(re.search(rf"(?<![a-z0-9_]){re.escape(p)}(?![a-z0-9_])", c) for p in PROTECTED_SCHEMAS)


def _backup_problem(it: dict) -> str | None:
    b, rb = it.get("backup"), it.get("rollback") or []
    action = str(it.get("action", ""))
    obj = str(it.get("object", "")).lower()
    creating = action.startswith("create")
    if not b or not isinstance(b, dict) or not b.get("type") or not rb:
        return "missing backup or rollback step"
    if b["type"] == "none_needed" and not creating:
        return "backup 'none_needed' on an object that already exists"
    if b["type"] == "backup_table":
        sql = _canon(b.get("sql", ""))
        if not (b.get("table") and re.search(rf"create\s+table\s+\S+\s+as\s+select\s+\*\s+from\s+{re.escape(obj)}\b",
                                             sql)):
            return "backup_table step does not copy the object"
    if b["type"] == "ddl_capture" and action == "replace_view" and not b.get("previous_sql"):
        return "replace without the previous definition captured"
    rbt = " ".join(rb)
    if not creating and re.search(r"\bdrop\b", rbt, re.I) and not re.search(r"\b(create|insert|restore)\b", rbt, re.I):
        return "rollback only drops the object - it would destroy, not restore"
    return None



# plan_token -> (root, lint-input hash) of the most recent lint; write_plan records it in the ledger
LAST_LINT_INPUTS: dict[str, tuple[str, str]] = {}


def _is_protected_job(name: str, j: dict) -> bool:
    owner = str((j.get("tags") or {}).get("owner", "")).strip().lower()
    return owner in PROTECTED_SCHEMAS or is_protected(name)


def _running_jobs(jobs: dict) -> list[tuple[str, str, set | None]]:
    """(name, state, uses_objects or None if unknown) for EVERY job: a paused or unscheduled job can be resumed
    or run by hand, so it still depends on what it reads."""
    out = []
    for name, j in jobs.items():
        sch, cont, trig = j.get("schedule") or {}, j.get("continuous"), j.get("trigger")
        if sch.get("quartz_cron_expression"):
            why = sch["quartz_cron_expression"] + (" PAUSED" if sch.get("pause_status") == "PAUSED" else "")
        elif isinstance(cont, dict):
            why = "continuous" + (" PAUSED" if cont.get("pause_status") == "PAUSED" else "")
        elif isinstance(trig, dict):
            why = "triggered" + (" PAUSED" if trig.get("pause_status") == "PAUSED" else "")
        else:
            why = "manual"
        out.append((name, why, _uses(j)))
    return out


def _uses(j: dict) -> set | None:
    tags = j.get("tags") or {}
    if "uses_objects" not in tags:
        return None
    return {o.strip().lower() for o in str(tags["uses_objects"]).split(",") if o.strip()}


def _dependency_graph(plan: dict, views: dict[str, str]) -> dict[str, set[str]]:
    deps = {fqn.lower(): sql_refs(view_body(sql)) for fqn, sql in views.items()}
    for it in plan.get("items", []):  # the plan's own new definitions count too
        if it.get("kind") == "view":
            for stmt in it.get("change", []):
                deps[str(it.get("object", "")).lower()] = sql_refs(view_body(stmt))
    return deps


def _closure(starts, deps) -> set[str]:
    seen, stack = set(), list(starts)
    while stack:
        o = stack.pop()
        if o in seen:
            continue
        seen.add(o)
        stack.extend(deps.get(o, ()))
    return seen


def _lint_context(tgt_ws: Workspace) -> tuple[dict, dict, str]:
    """Everything lint reads from the target, plus one hash over it (bound into the approval)."""
    jobs = tgt_ws.jobs()
    views = tgt_ws.views() if tgt_ws.db_path.exists() else {}
    return jobs, views, sha({"jobs": jobs, "views": views})


def lint_with_inputs(plan: dict, root=None) -> tuple[list[dict], str]:
    tgt_ws = workspaces(root)[plan["target"]]
    jobs, views, inputs_hash = _lint_context(tgt_ws)
    deps = _dependency_graph(plan, views)
    users: dict[str, list[str]] = {}
    unknown = []
    for name, why, uses in _running_jobs(jobs):
        if uses is None:
            unknown.append(f"{name} ({why})")
            continue
        for o in _closure(uses, deps):
            users.setdefault(o, []).append(f"{name} ({why})")
    # everything the protected team reads, directly or through views (paused or not)
    prot_starts = {v.lower() for v in views if is_protected(v)}
    for name, j in jobs.items():
        if _is_protected_job(name, j):
            prot_starts |= (_uses(j) or set())
    prot_reads = _closure(prot_starts, deps)
    prot_unknown = sorted(n for n, j in jobs.items() if _is_protected_job(n, j) and _uses(j) is None)
    findings = []
    guard = check_many([(stmt, str(it.get("object", "")).lower()) for it in plan.get("items", [])
                        if it.get("kind") in ("view", "config_table")
                        for stmt in it.get("change", []) + it.get("rollback", [])])

    def add(rule, sev, blocking, it, msg):
        findings.append({"rule": rule, "severity": sev, "blocking": blocking, "item": it.get("id"),
                         "message": f"{it.get('object')}: {msg}"})
    for it in plan.get("items", []):
        text = " ".join(it.get("change", []) + it.get("rollback", []))
        payload = it.get("payload") or {}
        ptext = json.dumps(payload) + (it.get("payload_text") or "")
        obj = str(it.get("object", "")).lower()
        owner = str((payload.get("tags") or {}).get("owner", "")).strip().lower()
        if is_protected(obj) or touches_protected(text) or touches_protected(ptext) or owner in PROTECTED_SCHEMAS:
            add("R2", "HIGH", True, it, "touches the protected team namespace - not allowed")
        elif it.get("kind") in ("view", "config_table") and (bad := sorted({
                p for stmt in it.get("change", []) + it.get("rollback", []) for p in guard[(stmt, obj)]})):
            add("R2", "HIGH", True, it, "SQL outside the plan allowlist (could reach the protected namespace or run "
                f"dynamic SQL): {bad}")
        elif obj in prot_reads:
            add("R2", "HIGH", True, it, "the protected team depends on this object (directly or through views) - "
                "changing it changes their results; agree it with them first")
        elif prot_unknown and it.get("kind") in ("view", "config_table"):
            add("R2", "HIGH", True, it, f"protected team dependencies unknown: job(s) {prot_unknown} declare no "
                "uses_objects, so any change may alter their results")
        prob = _backup_problem(it)
        if prob:
            add("R3", "HIGH", True, it, prob)
        if _REPLACES.search(" ".join(it.get("change", []))):
            if obj in users:
                add("R1", "HIGH", False, it, f"CREATE OR REPLACE / DROP on an object used (directly or through views) "
                    f"by job(s) {sorted(users[obj])} (paused jobs can be resumed) - likely to break a running job; split into its own PR "
                    f"and run in a paused window")
            elif unknown:
                add("R1", "HIGH", False, it, f"CREATE OR REPLACE / DROP while job(s) {unknown} declare no "
                    f"dependencies - impact unknown")
        if it.get("target_only_change"):
            add("R5", "HIGH", False, it, f"overwrites {str(plan.get('target')).upper()}-only change (a hotfix that "
                "exists in no other environment) - back-port it to the source or accept explicitly")
        removed = it.get("removes_keys") or []
        if removed or it.get("action") == "rebuild_table":
            what = f"removes config key(s) {removed}" if removed else "drops and rebuilds the table"
            add("R4", "MEDIUM", False, it, f"{what} - confirm intent")
    LAST_LINT_INPUTS[plan_token(plan)] = (str(root), inputs_hash)
    return findings, inputs_hash


def lint(plan: dict, root=None) -> list[dict]:
    return lint_with_inputs(plan, root)[0]


def split(plan: dict, findings: list[dict]) -> tuple[dict, dict]:
    risky = {f["item"] for f in findings if f["rule"] == "R1"}
    a = dict(plan, plan_id=plan["plan_id"] + "-a", items=[i for i in plan["items"] if i["id"] not in risky])
    b = dict(plan, plan_id=plan["plan_id"] + "-b", items=[i for i in plan["items"] if i["id"] in risky],
             notes=["Split out by risk linter R1: run with the dependent scheduled job paused."])
    return a, b


def to_markdown(plan: dict, findings: list[dict]) -> str:
    L = [f"# Alignment PR: {plan['source'].upper()} -> {plan['target'].upper()}  (`{plan['plan_id']}`)", "",
         "Contoso Insurance (fictional sample data). Generated by `envalign promote`.", "",
         f"Items: {len(plan['items'])}. Promotion path: feature branch -> PR to DEV -> PR to UAT -> PR to PROD.", "",
         "## Risk lint", ""]
    if not findings:
        L.append("No findings.")
    for f in findings:
        L.append(f"- **{f['rule']} {f['severity']}{' BLOCKING' if f['blocking'] else ''}** item {f['item']}: "
                 f"{f['message']}")
    L += ["", "## Plan-level backup (always, before any item)", "",
          "- Full copy of the target catalog file, jobs folder and bundle (the Delta time-travel analogue), "
          "with SHA-256 of every file in a backup register.", "- Baseline acceptance tests recorded before the change.",
          ""]
    for it in plan["items"]:
        L += [f"## {it['id']} {it['kind']} `{it['object']}` - {it['action']}", "",
              f"Why: {'; '.join(map(str, it.get('reason', [])))[:400]}", "",
              f"**Backup:** {it['backup'].get('type') if it.get('backup') else 'MISSING'}"
              f"{' -> ' + it['backup']['table'] if it.get('backup', {}).get('table') else ''}", "",
              "**Pre-tests:** " + "; ".join(it.get("pre_tests", [])), "", "**Change:**", "```sql"]
        L += [s if len(s) < 300 else s[:300] + " ..." for s in it.get("change", [])[:12]]
        if len(it.get("change", [])) > 12:
            L.append(f"-- ... {len(it['change']) - 12} more statements")
        L += ["```", "", "**Post-tests:** " + "; ".join(it.get("post_tests", [])), "", "**Rollback:**", "```sql"]
        L += it.get("rollback", []) + ["```", ""]
    if plan.get("notes"):
        L += ["## Notes (not actioned)", ""] + [f"- {n}" for n in plan["notes"]] + [""]
    L += ["## Approval", "", f"Approval token for exactly this plan: `{plan_token(plan)}`", "",
          f"Run: `python -m envalign apply --plan <file> --env {plan['target']} --approve {plan_token(plan)} --execute`"]
    return "\n".join(L) + "\n"


def write_plan(plan: dict, findings: list[dict], out_dir: Path, root=None) -> tuple[Path, Path]:
    """Write the reviewable plan and record it in the HMAC approvals ledger (outside the plan file)."""
    from .approvals import record
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = dict(plan)
    tok = plan_token(plan)
    cached = LAST_LINT_INPUTS.get(tok)
    if root is not None or cached is None:
        _, inputs = lint_with_inputs(plan, root)
        lroot = root
    else:
        lroot, inputs = (None if cached[0] == "None" else cached[0]), cached[1]
    record(plan, inputs, lroot)
    plan["approval_token"] = tok
    plan["lint"] = findings
    pj = out_dir / f"plan-{plan['plan_id']}.json"
    pm = out_dir / f"plan-{plan['plan_id']}.md"
    pj.write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    pm.write_text(to_markdown(plan, findings), encoding="utf-8")
    return pj, pm


def main(argv=None):
    ap = argparse.ArgumentParser(prog="envalign plan")
    ap.add_argument("--root")
    ap.add_argument("--source", default="dev")
    ap.add_argument("--target", default="uat")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "results" / "plans"))
    ap.add_argument("--split", action="store_true", help="move R1-flagged items into a separate PR")
    a = ap.parse_args(argv)
    plan = build_plan(a.root, a.source, a.target)
    findings = lint(plan, a.root)
    plans = split(plan, findings) if a.split else (plan,)
    for p in plans:
        if not p["items"]:
            continue
        f = lint(p, a.root)
        pj, pm = write_plan(p, f, Path(a.out), a.root)
        print(f"{pm}  items={len(p['items'])}  findings={[x['rule'] for x in f]}  token={plan_token(p)}")
