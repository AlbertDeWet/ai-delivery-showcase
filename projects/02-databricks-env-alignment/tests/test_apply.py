import hashlib
import json

import duckdb
from pathlib import Path

from envalign.apply import apply
from envalign.envdiff import compare, matrix, protected_fingerprint, snapshot
from envalign.promote import build_plan, lint, plan_token, split, write_plan
from envalign.workspace import workspaces


def _h(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _plan_file(root, tmp_path, src="dev", tgt="uat", part=None):
    plan = build_plan(root, src, tgt)
    if part is not None:
        plan = split(plan, lint(plan, root))[part]
    pj, _ = write_plan(plan, lint(plan, root), tmp_path / "plans")
    return pj, plan_token(json.loads(pj.read_text()))


def _accepts(pj):
    plan = json.loads(pj.read_text())
    return tuple(f"{x['rule']}:{x['item']}" for x in plan["lint"] if x["severity"] == "HIGH" and not x["blocking"])


def test_dry_run_is_default_and_changes_nothing(root, tmp_path, logdir):
    ws = workspaces(root)["uat"]
    before = _h(ws.db_path)
    pj, tok = _plan_file(root, tmp_path, part=0)
    res = apply(pj, "uat", approve=tok, root=root, log_dir=logdir)  # token but no --execute
    assert res["status"] == "dry-run"
    assert _h(ws.db_path) == before


def test_wrong_token_and_wrong_env_refused(root, tmp_path, logdir):
    pj, tok = _plan_file(root, tmp_path, part=0)
    assert apply(pj, "uat", approve="APPROVE-0000", execute=True, root=root, log_dir=logdir)["status"] == "refused"
    assert apply(pj, "prod", approve=tok, execute=True, root=root, log_dir=logdir)["status"] == "refused"


def test_tampered_plan_refused(root, tmp_path, logdir):
    pj, tok = _plan_file(root, tmp_path, part=0)
    plan = json.loads(pj.read_text())
    plan["items"][0]["change"].append("DELETE FROM core.policy")
    pj.write_text(json.dumps(plan))
    assert apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)["status"] == "refused"


def test_unaccepted_r1_refused_then_accepted_applies(root, tmp_path, logdir):
    pj, tok = _plan_file(root, tmp_path, part=1)
    assert apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)["status"] == "refused"
    assert apply(pj, "uat", approve=tok, execute=True, accept_risks=("R1",), root=root,
                 log_dir=logdir)["status"] == "refused"  # bare rule name is not an acceptance
    res = apply(pj, "uat", approve=tok, execute=True, accept_risks=_accepts(pj), root=root, log_dir=logdir)
    assert res["status"] == "applied", res


def test_blocking_r2_cannot_be_accepted(root, tmp_path, logdir):
    plan = build_plan(root, "dev", "uat")
    plan["items"][0]["change"].append("DELETE FROM team_b.ingest_log")
    pj, _ = write_plan(plan, [], tmp_path / "plans", root)
    tok = plan_token(json.loads(pj.read_text()))
    res = apply(pj, "uat", approve=tok, execute=True, accept_risks=("R1", "R2", "R3"), root=root, log_dir=logdir)
    assert res["status"] == "refused" and "protected" in res["reason"]


def test_stale_plan_refused(root, tmp_path, logdir):
    pj, tok = _plan_file(root, tmp_path, part=0)
    ws = workspaces(root)["uat"]
    con = duckdb.connect(str(ws.db_path))
    con.execute("INSERT INTO core.cfg_fx_source VALUES ('CHF', 'someone_changed_it', TRUE)")
    con.close()
    res = apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "refused" and "changed since plan" in res["reason"]


def test_full_alignment_backup_tests_and_protected_untouched(root, tmp_path, logdir):
    ws = workspaces(root)
    prot = protected_fingerprint(ws["uat"])
    for part in (0, 1):
        pj, tok = _plan_file(root, tmp_path, part=part)
        res = apply(pj, "uat", approve=tok, execute=True, accept_risks=_accepts(pj), root=root, log_dir=logdir)
        assert res["status"] == "applied", res
        assert res["tests_before"]["T2_changed_objects_match_source"] is False
        assert all(res["tests_after"].values())
        reg = json.loads((Path(res["backup_dir"]) / "backup-register.json").read_text())
        assert "catalog.duckdb" in reg["files"]
    m = matrix(compare(snapshot(ws["dev"]), snapshot(ws["uat"])))
    for k in ("view", "config_table", "job", "bundle"):
        assert m[k]["drift"] == 0 and m[k]["missing"] == 0, (k, m[k])
    assert protected_fingerprint(ws["uat"]) == prot


def test_failure_auto_rolls_back(root, tmp_path, logdir):
    ws = workspaces(root)["uat"]
    before = {p.name: _h(p) for p in [ws.db_path, ws.bundle_path, *ws.jobs_dir.glob("*.json")]}
    plan = build_plan(root, "dev", "uat")
    plan = split(plan, lint(plan, root))[0]
    plan["items"].append({"id": "999", "kind": "view", "object": "core.v_boom", "action": "create_view",
                          "backup": {"type": "ddl_capture"}, "change": [
                              "CREATE VIEW IF NOT EXISTS core.v_boom AS SELECT error('boom') AS x"],
                          "rollback": ["DROP VIEW IF EXISTS core.v_boom"], "pre_tests": [], "post_tests": [],
                          "target_fingerprint": None, "expected_fingerprint": None})
    pj, _ = write_plan(plan, [], tmp_path / "plans", root)
    tok = plan_token(json.loads(pj.read_text()))
    res = apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "rolled_back" and res["restore_verified"]
    assert "T1_core_views_queryable" in res["failures"]
    after = {p.name: _h(p) for p in [ws.db_path, ws.bundle_path, *ws.jobs_dir.glob("*.json")]}
    assert after == before


def test_forged_stored_token_refused(root, tmp_path, logdir):
    pj, _ = _plan_file(root, tmp_path, part=0)
    plan = json.loads(pj.read_text())
    plan["items"][0]["change"].append("SELECT 1")
    plan["approval_token"] = plan_token(plan)  # attacker recomputes everything in the file
    pj.write_text(json.dumps(plan))
    res = apply(pj, "uat", approve=plan["approval_token"], execute=True, root=root, log_dir=logdir)
    assert res["status"] == "refused" and "ledger" in res["reason"]


def test_lint_inputs_changed_after_review_refused(root, tmp_path, logdir):
    pj, tok = _plan_file(root, tmp_path, part=0)
    ws = workspaces(root)["uat"]
    jp = ws.jobs_dir / "claims_daily.json"
    j = json.loads(jp.read_text())
    j["tags"]["uses_objects"] += ",core.v_policy_active"
    jp.write_text(json.dumps(j))
    res = apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "refused" and "changed since the plan was reviewed" in res["reason"]


def test_rollback_partial_is_reported_not_silent(root, tmp_path, logdir, monkeypatch):
    """If a file cannot be restored, apply must say ROLLBACK_PARTIAL with details - never claim success."""
    import envalign.apply as ap
    ws = workspaces(root)["uat"]
    plan = build_plan(root, "dev", "uat", kinds=("job",))
    assert plan["items"]
    plan["items"].append({"id": "999", "kind": "view", "object": "core.v_boom", "action": "create_view",
                          "backup": {"type": "ddl_capture"}, "change": [
                              "CREATE VIEW IF NOT EXISTS core.v_boom AS SELECT error('boom') AS x"],
                          "rollback": ["DROP VIEW IF EXISTS core.v_boom"], "pre_tests": [], "post_tests": [],
                          "target_fingerprint": None, "expected_fingerprint": None})
    pj, _ = write_plan(plan, lint(plan, root), tmp_path / "plans", root)
    job = plan["items"][0]["object"]
    real = ap._exec_file_items

    def sabotage(ws_, items):
        real(ws_, items)
        for bak in (root / "_backups" / "uat").glob(f"*/jobs/{job}.json"):
            bak.unlink()  # the backup copy of a changed file is lost
    monkeypatch.setattr(ap, "_exec_file_items", sabotage)
    res = apply(pj, "uat", approve=plan_token(plan), execute=True, root=root, log_dir=logdir)
    assert res["status"] == "ROLLBACK_PARTIAL"
    assert res["restore_verified"] is False
    assert any(job in e for e in res["restore_errors"])
    assert (ws.path / "catalog.duckdb").exists()


def test_approval_is_single_use(root, tmp_path, logdir):
    pj, tok = _plan_file(root, tmp_path, part=0)
    assert apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)["status"] == "applied"
    res = apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "refused"
