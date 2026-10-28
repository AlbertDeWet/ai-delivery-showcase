"""envdiff - fingerprint two workspaces and classify every difference.

Statuses per object:
  identical  equal after cosmetic normalisation
  expected   differs only in per-environment tokens/fields (allowed)
  drift      real difference
  missing    exists in the left env only (missing in the right)
  extra      exists in the right env only
Protected-team objects are fingerprinted and reported separately, never mixed into the matrix.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .normalise import (EXPECTED_ENV_TABLES, env_mask, env_tokens, json_diff_paths, norm_job, norm_sql, row_hash, sha)
from .workspace import Workspace, is_protected, workspaces

KINDS = ("view", "config_table", "job", "bundle")


@dataclass
class Diff:
    kind: str
    obj: str
    status: str
    detail: list = field(default_factory=list)
    protected: bool = False


def snapshot(ws: Workspace, con=None) -> dict:
    own = con is None
    con = con or ws.connect()
    try:
        views = ws.views(con)
        cfgs = ws.config_tables(con)
        try:
            prot_data = sha(con.execute("SELECT * FROM team_b.ingest_log ORDER BY ALL").fetchall())
        except Exception:  # noqa: BLE001 - schema absent
            prot_data = None
    finally:
        if own:
            con.close()
    snap = {"env": ws.env, "view": {}, "config_table": {}, "job": {}, "bundle": {}, "_protected_data": prot_data,
            "_cfg_raw": cfgs}
    for fqn, sql in views.items():
        snap["view"][fqn] = {"sql": sql, "norm": norm_sql(sql, mask_env=False), "norm_env": norm_sql(sql)}
    for fqn, t in cfgs.items():
        rows = {}
        for r in t["rows"]:
            k = str(r.get("key"))
            rows[k] = {"h": row_hash(r), "h_env": row_hash(r, mask_env=True)}
        snap["config_table"][fqn] = {"columns": t["columns"], "rows": rows,
                                     "schema_hash": sha(t["columns"]),
                                     "hash": sha({"c": t["columns"], "r": {k: v["h"] for k, v in rows.items()}})}
    for name, j in ws.jobs().items():
        snap["job"][name] = {"raw": j, "norm": norm_job(j, "identical"), "norm_env": norm_job(j, "expected")}
    b = ws.bundle()
    for sec, val in b.items():
        if sec == "targets" and isinstance(val, dict):
            for t, tv in val.items():
                snap["bundle"][f"targets.{t}"] = {"hash": sha(tv), "val": tv}
        else:
            snap["bundle"][sec] = {"hash": sha(val), "val": val}
    return snap


def fingerprint(snap: dict, kind: str, obj: str) -> str | None:
    """Stable hash of one object as found in a snapshot (None if absent). Used by plan/apply stale checks."""
    o = snap[kind].get(obj)
    if o is None:
        return None
    if kind == "view":
        return sha(o["norm"])
    if kind == "config_table":
        return o["hash"]
    if kind == "job":
        return sha(o["norm"])
    return o["hash"]


def _compare_view(a, b):
    if a["norm"] == b["norm"]:
        return "identical", []
    if a["norm_env"] == b["norm_env"]:
        return "expected", ["env tokens only"]
    return "drift", [f"left:  {a['norm'][:160]}", f"right: {b['norm'][:160]}"]


def _compare_cfg(fqn, a, b):
    if a["hash"] == b["hash"]:
        return "identical", []
    detail = []
    if a["schema_hash"] != b["schema_hash"]:
        detail.append(f"schema differs: {[c[0] for c in a['columns']]} vs {[c[0] for c in b['columns']]}")
    ka, kb = set(a["rows"]), set(b["rows"])
    miss, extra = sorted(ka - kb), sorted(kb - ka)
    changed = sorted(k for k in ka & kb if a["rows"][k]["h"] != b["rows"][k]["h"])
    changed_real = [k for k in changed if a["rows"][k]["h_env"] != b["rows"][k]["h_env"]]
    if miss:
        detail.append(f"keys missing in right: {miss}")
    if extra:
        detail.append(f"keys extra in right: {extra}")
    if changed:
        detail.append(f"keys with different values: {changed}")
    if fqn in EXPECTED_ENV_TABLES and a["schema_hash"] == b["schema_hash"]:
        return "expected", detail + ["table is environment-specific by design"]
    if a["schema_hash"] == b["schema_hash"] and not miss and not extra and not changed_real:
        return "expected", detail + ["env tokens only"]
    return "drift", detail


def _compare_job(a, b):
    if a["norm"] == b["norm"]:
        return "identical", []
    paths = json_diff_paths(a["norm"], b["norm"])
    if a["norm_env"] == b["norm_env"]:
        return "expected", paths
    return "drift", json_diff_paths(a["norm_env"], b["norm_env"])


def compare(left: dict, right: dict) -> list[Diff]:
    out: list[Diff] = []
    for kind in KINDS:
        L, R = left[kind], right[kind]
        for obj in sorted(set(L) | set(R)):
            prot = is_protected(obj) or (kind == "job" and
                                         ((L.get(obj) or R.get(obj))["raw"].get("tags", {}).get("owner") == "team_b"))
            if obj not in R:
                out.append(Diff(kind, obj, "missing", [f"only in {left['env']}"], prot))
                continue
            if obj not in L:
                out.append(Diff(kind, obj, "extra", [f"only in {right['env']}"], prot))
                continue
            if kind == "view":
                st, d = _compare_view(L[obj], R[obj])
            elif kind == "config_table":
                st, d = _compare_cfg(obj, L[obj], R[obj])
            elif kind == "job":
                st, d = _compare_job(L[obj], R[obj])
            else:
                if L[obj]["hash"] == R[obj]["hash"]:
                    st, d = "identical", []
                elif sha(env_mask(json.dumps(L[obj]["val"], sort_keys=True))) == \
                        sha(env_mask(json.dumps(R[obj]["val"], sort_keys=True))):
                    st, d = "expected", ["env tokens only"]
                else:
                    st, d = "drift", json_diff_paths(L[obj]["val"], R[obj]["val"]) or ["value differs"]
            if st in ("identical", "expected") and kind != "bundle":
                extra = _env_problems(kind, obj, left, right)
                if extra:
                    st, d = "drift", d + extra
            out.append(Diff(kind, obj, st, d, prot))
    return out


def _obj_text(snap: dict, kind: str, obj: str) -> str:
    o = snap[kind][obj]
    if kind == "view":
        return o["sql"]
    if kind == "job":
        return json.dumps(o["raw"])
    raw = (snap.get("_cfg_raw") or {}).get(obj)
    return json.dumps(raw["rows"], default=str) if raw else ""


def _env_problems(kind: str, obj: str, left: dict, right: dict) -> list[str]:
    """Problems that make an otherwise equal pair real drift: wrong-env references, paused prod jobs."""
    probs = []
    for snap in (left, right):
        env = snap.get("env")
        wrong = env_tokens(_obj_text(snap, kind, obj)) - {env}
        if wrong:
            probs.append(f"cross-environment reference in {env}: points at {sorted(wrong)}")
        if kind == "job" and env == "prod":
            raw = snap["job"][obj]["raw"]
            for blk in ("schedule", "continuous"):
                if (raw.get(blk) or {}).get("pause_status") == "PAUSED":
                    probs.append(f"prod job is PAUSED ({blk}) - production would silently stop")
    return probs


def matrix(diffs: list[Diff]) -> dict:
    m = {}
    for kind in KINDS:
        ds = [d for d in diffs if d.kind == kind and not d.protected]
        c = {s: sum(1 for d in ds if d.status == s) for s in ("identical", "expected", "drift", "missing", "extra")}
        c["total"] = len(ds)
        c["aligned"] = c["identical"] + c["expected"]
        m[kind] = c
    prot = [d for d in diffs if d.protected]
    m["protected"] = {"objects": len(prot), "differences": sum(1 for d in prot if d.status != "identical"),
                      "note": "reported only - never planned or touched"}
    return m


def protected_fingerprint(ws: Workspace, snap: dict | None = None, con=None) -> str:
    """The protected team's own check: one hash over every team_b object (and its data) in one env."""
    s = snap or snapshot(ws, con)
    parts = {k: {o: fingerprint(s, k, o) for o in s[k] if is_protected(o) or
                 (k == "job" and s[k][o]["raw"].get("tags", {}).get("owner") == "team_b")} for k in KINDS}
    parts["data"] = s["_protected_data"]
    return sha(parts)


def run(root=None, left="dev", right="uat") -> dict:
    ws = workspaces(root)
    diffs = compare(snapshot(ws[left]), snapshot(ws[right]))
    return {"left": left, "right": right, "matrix": matrix(diffs), "diffs": [asdict(d) for d in diffs]}


def format_matrix(res: dict) -> str:
    lines = [f"Alignment {res['left'].upper()} -> {res['right'].upper()}",
             f"{'type':<14}{'identical':>11}{'aligned*':>10}{'drift':>7}{'missing':>9}{'extra':>7}"]
    for kind in KINDS:
        c = res["matrix"][kind]
        lines.append(f"{kind:<14}{c['identical']:>4} of {c['total']:<4}{c['aligned']:>4} of {c['total']:<3}"
                     f"{c['drift']:>5}{c['missing']:>9}{c['extra']:>7}")
    p = res["matrix"]["protected"]
    lines.append(f"protected team: {p['objects']} objects, {p['differences']} differ (excluded, not touched)")
    lines.append("* aligned = identical + expected per-environment difference")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="envalign diff")
    ap.add_argument("--root")
    ap.add_argument("--left", default="dev")
    ap.add_argument("--right", default="uat")
    ap.add_argument("--json", help="write full result to this file")
    ap.add_argument("--show", choices=["drift", "all"], default="drift")
    a = ap.parse_args(argv)
    res = run(a.root, a.left, a.right)
    print(format_matrix(res))
    for d in res["diffs"]:
        if a.show == "all" or d["status"] in ("drift", "missing", "extra"):
            tag = " [PROTECTED]" if d["protected"] else ""
            print(f"  {d['status']:<9} {d['kind']:<13} {d['obj']}{tag}  {'; '.join(map(str, d['detail']))[:200]}")
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    return res
