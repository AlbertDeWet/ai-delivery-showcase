"""Adversarial tests, Round 2 (role: ADVERSARY). Attacks the Round 1 fixes.

These encode CORRECT behaviour; a failing test is an open finding.
"""
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import duckdb
import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from envalign.apply import apply  # noqa: E402
from envalign.envdiff import compare  # noqa: E402
from envalign.normalise import env_substitute, norm_sql  # noqa: E402
from envalign.promote import build_plan, lint, plan_token, split, write_plan  # noqa: E402
from envalign.workspace import workspaces  # noqa: E402


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def _tree(path: Path) -> dict:
    return {str(p.relative_to(path)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def _empty_target(tmp_path, env="uat", jobs=None):
    root = tmp_path / "ws"
    (root / env / "jobs").mkdir(parents=True)
    for name, j in (jobs or {}).items():
        (root / env / "jobs" / f"{name}.json").write_text(json.dumps(j), encoding="utf-8")
    return root


def _plan(items, target="uat"):
    return {"plan_id": "adv2", "source": "dev", "target": target, "items": items}


def _view_item(obj, change, iid="001", action="create_view"):
    return {"id": iid, "kind": "view", "object": obj, "action": action, "change": change,
            "rollback": [f"DROP VIEW IF EXISTS {obj}"], "backup": {"type": "ddl_capture", "object": obj}}


def _job(name, uses, owner="reporting", paused="UNPAUSED"):
    return {"name": name, "tags": {"owner": owner, "uses_objects": ",".join(uses)},
            "schedule": {"quartz_cron_expression": "0 0/30 * * * ?", "pause_status": paused},
            "tasks": [{"task_key": "t", "notebook_task": {"notebook_path": "/x"}}]}


def _view_snap(env, views):
    return {"env": env, "view": {k: {"sql": v, "norm": norm_sql(v, mask_env=False), "norm_env": norm_sql(v)}
                                 for k, v in views.items()},
            "config_table": {}, "job": {}, "bundle": {}}


def _set_view(ws, name, sql):
    con = duckdb.connect(str(ws.db_path))
    try:
        con.execute(f"CREATE OR REPLACE VIEW {name} AS {sql}")
    finally:
        con.close()


def _bad_view_item():
    return {"id": "999", "kind": "view", "object": "core.v_adv_bad", "action": "create_view",
            "change": ["CREATE VIEW core.v_adv_bad AS SELECT CAST('not a number' AS INTEGER) AS v"],
            "rollback": ["DROP VIEW IF EXISTS core.v_adv_bad"], "backup": {"type": "ddl_capture"},
            "target_fingerprint": None, "expected_fingerprint": None}


def _accept_all(plan, root):
    return tuple(f"{f['rule']}:{f['item']}" for f in lint(plan, root) if f["severity"] == "HIGH")


# ----------------------------------------------------------------------------------------------
# R2 bypasses: dynamic SQL, concatenation, wrapping views, shared objects
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("stmt", [
    "EXECUTE IMMEDIATE 'DELETE FROM team' || '_b.ingest_log'",
    "EXECUTE IMMEDIATE concat('DROP TABLE ', 'team_', 'b', '.ingest_log')",
    "SELECT * FROM query('SELECT * FROM ' || 'team' || '_b.ingest_log')",
    "EXECUTE IMMEDIATE replace('DELETE FROM teamXb.ingest_log', 'X', '_')",
])
def test_r2_dynamic_sql_is_blocked(tmp_path, stmt):
    """ADVERSARY: dynamic SQL (EXECUTE IMMEDIATE / query()) assembled by string concatenation reaches team_b
    without ever spelling 'team_b'. Any dynamic SQL in a plan must be blocking (it cannot be proven safe)."""
    f = lint(_plan([_view_item("core.v_x", [stmt])]), _empty_target(tmp_path))
    assert any(x["rule"] == "R2" and x["blocking"] for x in f), stmt


def test_r2_replacing_a_core_view_the_protected_team_reads(tmp_path):
    """ADVERSARY: team_b's own running job depends on core.v_shared. Replacing core.v_shared changes the
    protected team's results although no statement names team_b. That is a touch on the protected team."""
    root = _empty_target(tmp_path, jobs={"team_b_ingest": _job("team_b_ingest", ["core.v_shared"], owner="team_b",
                                                               paused="PAUSED")})
    it = _view_item("core.v_shared", ["CREATE OR REPLACE VIEW core.v_shared AS SELECT 2 AS x"],
                    action="replace_view")
    it["backup"]["previous_sql"] = "CREATE VIEW core.v_shared AS SELECT 1 AS x"
    it["rollback"] = ["CREATE OR REPLACE VIEW core.v_shared AS SELECT 1 AS x"]
    f = lint(_plan([it]), root)
    assert any(x["rule"] == "R2" for x in f), [x["rule"] for x in f]


def test_r2_replacing_core_view_wrapped_by_team_b_view(root):
    """ADVERSARY: team_b.v_wrap is a protected view that SELECTs FROM core.v_large_claims. Replacing the core
    view silently changes team_b's view. The linter must see the reverse dependency."""
    ws = workspaces(root)["uat"]
    _set_view(ws, "team_b.v_wrap", "SELECT * FROM core.v_large_claims")
    it = _view_item("core.v_large_claims",
                    ["CREATE OR REPLACE VIEW core.v_large_claims AS SELECT * FROM core.claim WHERE amount >= 1"],
                    action="replace_view")
    it["backup"]["previous_sql"] = "SELECT * FROM core.claim WHERE amount >= 50000"
    it["rollback"] = ["CREATE OR REPLACE VIEW core.v_large_claims AS SELECT * FROM core.claim WHERE amount >= 50000"]
    f = lint(_plan([it]), root)
    assert any(x["rule"] == "R2" for x in f), [x["rule"] for x in f]


# ----------------------------------------------------------------------------------------------
# Token binding
# ----------------------------------------------------------------------------------------------
def test_forged_plan_with_rewritten_stored_token_is_refused(root, tmp_path, logdir):
    """ADVERSARY: the F1 fix compares the stored approval_token with a recomputed one, but both live in the same
    editable file. Edit the plan, rewrite its approval_token with plan_token(edited) and pass that: the forged
    plan passes every token check. A token anyone can compute from the file is not an approval."""
    plan = build_plan(root, "dev", "uat")
    a, _ = split(plan, lint(plan, root))
    pj, _ = write_plan(a, lint(a, root), tmp_path / "plans")
    reviewed = json.loads(pj.read_text(encoding="utf-8"))["approval_token"]
    p = json.loads(pj.read_text(encoding="utf-8"))
    cfg = next(i for i in p["items"] if i["kind"] == "config_table")
    cfg["change"].append(f"UPDATE {cfg['object']} SET active = FALSE")
    p["approval_token"] = plan_token(p)
    pj.write_text(json.dumps(p), encoding="utf-8")
    assert p["approval_token"] != reviewed
    res = apply(pj, "uat", approve=p["approval_token"], execute=False, root=root, log_dir=logdir)
    assert res["status"] == "refused", "forged plan accepted with a self-computed token"


def test_token_binds_job_definitions_lint_read(root, tmp_path, logdir):
    """ADVERSARY: after approval (R1 accepted for the reviewed job list), a NEW running job that reads the same
    view is deployed to UAT. Lint now names a job the reviewer never saw, but the finding key RULE:ITEM is
    unchanged so the old acceptance covers it and apply runs. The token must cover the job definitions lint
    read; a change to them must refuse."""
    plan = build_plan(root, "dev", "uat")
    _, b = split(plan, lint(plan, root))
    assert b["items"], "expected an R1 item dev->uat"
    pj, _ = write_plan(b, lint(b, root), tmp_path / "plans")
    stored = json.loads(pj.read_text(encoding="utf-8"))["approval_token"]
    acc = _accept_all(b, root)
    ws = workspaces(root)["uat"]
    obj = b["items"][0]["object"]
    (ws.jobs_dir / "payments_critical.json").write_text(
        json.dumps(_job("payments_critical", [obj])), encoding="utf-8")
    res = apply(pj, "uat", approve=stored, execute=False, accept_risks=acc, root=root, log_dir=logdir)
    assert res["status"] == "refused", "job definitions changed after approval but the approval still held"


def test_token_binds_job_tags_removed_after_approval(root, tmp_path, logdir):
    """ADVERSARY: after approval the uses_objects tag of the job is cleared, so R1 disappears and apply no longer
    even needs the acceptance. Approval must be invalidated by any change to what lint read."""
    plan = build_plan(root, "dev", "uat")
    _, b = split(plan, lint(plan, root))
    pj, _ = write_plan(b, lint(b, root), tmp_path / "plans")
    stored = json.loads(pj.read_text(encoding="utf-8"))["approval_token"]
    ws = workspaces(root)["uat"]
    jp = ws.jobs_dir / "reporting_views_refresh.json"
    j = json.loads(jp.read_text(encoding="utf-8"))
    j["tags"]["uses_objects"] = ""
    jp.write_text(json.dumps(j), encoding="utf-8")
    res = apply(pj, "uat", approve=stored, execute=False, root=root, log_dir=logdir)
    assert res["status"] == "refused", "lint inputs tampered after approval; dry-run still passes"


# ----------------------------------------------------------------------------------------------
# Rollback: read-only files and partial failure
# ----------------------------------------------------------------------------------------------
def _ro(p: Path):
    os.chmod(p, stat.S_IREAD)


def _rw_all(path: Path):
    for p in path.rglob("*"):
        try:
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
        except OSError:
            pass


def test_rollback_with_readonly_unrelated_file(root, tmp_path, logdir):
    """ADVERSARY: the workspace holds a read-only file the plan never touches. A failing change triggers the
    restore, which copies EVERY registered file back - onto the read-only one - and crashes. Apply must still end
    'rolled_back' with the workspace byte-identical (or refuse before changing anything)."""
    ws = workspaces(root)["uat"]
    note = ws.path / "OWNERS.txt"
    note.write_text("owned by platform team\n", encoding="utf-8")
    _ro(note)
    try:
        before = _tree(ws.path)
        plan = {"plan_id": "adv2-ro", "source": "dev", "target": "uat", "items": [_bad_view_item()], "notes": []}
        pj, _ = write_plan(plan, lint(plan, root), tmp_path / "plans")
        try:
            res = apply(pj, "uat", approve=plan_token(plan), execute=True, root=root, log_dir=logdir)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"apply crashed during rollback: {type(e).__name__}: {e}")
        assert res["status"] in ("rolled_back", "refused")
        assert _tree(ws.path) == before
    finally:
        _rw_all(ws.path)


def test_partial_failure_readonly_job_file(root, tmp_path, logdir):
    """ADVERSARY: the DB change commits, then writing a read-only job JSON fails (partial failure). Restore must
    put the DB back and not crash on the same read-only file."""
    ws = workspaces(root)["uat"]
    plan = build_plan(root, "dev", "uat", kinds=("config_table", "job"))
    job_items = [i for i in plan["items"] if i["kind"] == "job"]
    assert job_items, "expected a job item dev->uat"
    jf = ws.jobs_dir / f"{job_items[0]['object']}.json"
    _ro(jf)
    try:
        before = _tree(ws.path)
        pj, _ = write_plan(plan, lint(plan, root), tmp_path / "plans")
        try:
            res = apply(pj, "uat", approve=plan_token(plan), execute=True, accept_risks=_accept_all(plan, root),
                        root=root, log_dir=logdir)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"apply crashed after a partial failure: {type(e).__name__}: {e}")
        assert res["status"] in ("rolled_back", "refused")
        assert _tree(ws.path) == before
    finally:
        _rw_all(ws.path)


# ----------------------------------------------------------------------------------------------
# R5 false positives
# ----------------------------------------------------------------------------------------------
def test_r5_not_raised_when_target_differs_only_by_env_token(root):
    """ADVERSARY: DEV improves a view that embeds its own env path. UAT and PROD both hold the OLD version, each
    with their own env token - so UAT's version is not a hotfix (PROD has the same thing, env-masked). R5 compares
    unmasked fingerprints and calls UAT's copy 'exists in no other environment'."""
    w = workspaces(root)
    for env in ("dev", "uat", "prod"):
        flt = " WHERE amount > 0" if env == "dev" else ""
        _set_view(w[env], "core.v_adv_src", f"SELECT *, 'lake://contoso_{env}/landing' AS src FROM core.claim{flt}")
    plan = build_plan(root, "dev", "uat")
    it = next(i for i in plan["items"] if i["object"] == "core.v_adv_src")
    assert not it.get("target_only_change")
    assert "R5" not in {f["rule"] for f in lint(plan, root) if f["item"] == it["id"]}


def test_r5_not_raised_for_cosmetic_target_copy(root):
    """ADVERSARY: UAT's copy differs from PROD's only cosmetically (case/whitespace) - same version, not a
    hotfix. R5 should not fire."""
    w = workspaces(root)
    _set_view(w["dev"], "core.v_adv_c", "SELECT claim_id FROM core.claim WHERE amount > 10")
    _set_view(w["uat"], "core.v_adv_c", "select claim_id   from core.claim")
    _set_view(w["prod"], "core.v_adv_c", "SELECT claim_id FROM core.claim")
    plan = build_plan(root, "dev", "uat")
    it = next(i for i in plan["items"] if i["object"] == "core.v_adv_c")
    assert not it.get("target_only_change")


# ----------------------------------------------------------------------------------------------
# Cross-environment rewrite errors
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "COPY t TO '/dev/null'",
    "/Workspace/Shared/dev/tools/helper",
    "owner: devops-dev@contoso-insurance.example",
])
def test_env_substitute_leaves_non_env_dev_alone(text):
    """ADVERSARY: '/dev/' in /dev/null or a shared 'dev' tools folder is not an environment name; promotion
    rewrites it to '/uat/'. (The third case is a real per-env mailbox and SHOULD be rewritten - expected PASS.)"""
    out = env_substitute(text, "dev", "uat")
    if "@" in text:
        assert out == "owner: devops-uat@contoso-insurance.example"
    else:
        assert out == text


def test_dev_null_in_uat_view_is_not_cross_env_drift():
    """ADVERSARY: a UAT view writing to /dev/null is flagged as referring to the DEV environment."""
    sql = "SELECT '/dev/null' AS sink FROM core.claim"
    diffs = compare(_view_snap("dev", {"core.v": sql}), _view_snap("uat", {"core.v": sql}))
    assert next(d.status for d in diffs if d.obj == "core.v") == "identical"


def test_word_containing_dev_not_rewritten():
    """ADVERSARY: baseline - 'device', 'devops', 'contoso_development' are not env tokens."""
    t = "SELECT device, devops FROM contoso_development.core.t"
    assert env_substitute(t, "dev", "uat") == t


# ----------------------------------------------------------------------------------------------
# Performance / timeouts
# ----------------------------------------------------------------------------------------------
def test_lint_scales_to_large_plan(tmp_path):
    """ADVERSARY: linting a 3000-item plan (big catch-up promotion) with a running untagged job finishes in under
    10 s; the dependency walk and canonicalisation must not be quadratic."""
    root = _empty_target(tmp_path, jobs={"j": _job("j", [f"core.v_{i}" for i in range(0, 3000, 7)])})
    items = [_view_item(f"core.v_{i}", [f"CREATE OR REPLACE VIEW core.v_{i} AS SELECT * FROM core.v_{i + 1}"],
                        iid=f"{i:04d}", action="replace_view") for i in range(3000)]
    t = time.perf_counter()
    lint(_plan(items), root)
    assert time.perf_counter() - t < 10


def test_normaliser_pathological_input_is_fast():
    """ADVERSARY: an unterminated quote / comment in a 1 MB view body must not hang the detector."""
    big = "SELECT " + ("a.b, " * 100000) + "'unterminated " + '"x' * 50000 + " /* open"
    t = time.perf_counter()
    norm_sql(big)
    assert time.perf_counter() - t < 5


def test_apply_dry_run_is_quick(root, tmp_path, logdir):
    """ADVERSARY: a dry run (lint + snapshot, no change) on the seeded estate completes in under 30 s."""
    plan = build_plan(root, "dev", "uat")
    a, _ = split(plan, lint(plan, root))
    pj, _ = write_plan(a, lint(a, root), tmp_path / "plans")
    t = time.perf_counter()
    res = apply(pj, "uat", root=root, log_dir=logdir)
    assert res["status"] == "dry-run"
    assert time.perf_counter() - t < 30
