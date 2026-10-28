"""Adversarial tests (role: ADVERSARY) against plan/apply on a seeded workspace copy (uses tests/conftest.py)."""
import hashlib
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from envalign.apply import apply  # noqa: E402
from envalign.promote import build_plan, lint, plan_token, split, write_plan  # noqa: E402
from envalign.workspace import workspaces  # noqa: E402


def _tree(path: Path) -> dict:
    return {str(p.relative_to(path)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()}


def _safe_plan(root, tmp_path, source="dev", target="uat"):
    plan = build_plan(root, source, target)
    a, _ = split(plan, lint(plan, root))
    pj, _ = write_plan(a, lint(a, root), tmp_path / "plans")
    return pj


def test_identical_state_gives_identical_token(root):
    """ADVERSARY: planning the same unchanged state twice must yield the same approval token; today the token
    hashes created_utc, so a reviewer's token for the plan they read never matches a regenerated plan."""
    t1 = plan_token(build_plan(root, "dev", "uat"))
    time.sleep(1.2)
    t2 = plan_token(build_plan(root, "dev", "uat"))
    assert t1 == t2


def test_tampered_plan_with_recomputed_token_is_refused(root, tmp_path, logdir):
    """ADVERSARY: after review the plan JSON is edited (an extra DELETE added). The file still carries the
    reviewed approval_token, but apply only checks the token the caller passes against the edited file, so a
    token recomputed from the tampered plan is accepted and the unreviewed SQL runs."""
    pj = _safe_plan(root, tmp_path)
    plan = json.loads(pj.read_text(encoding="utf-8"))
    reviewed = plan["approval_token"]
    cfg = next(i for i in plan["items"] if i["kind"] == "config_table")
    cfg["change"].append(f"DELETE FROM {cfg['object']}")
    pj.write_text(json.dumps(plan), encoding="utf-8")
    forged = plan_token(plan)
    assert forged != reviewed
    res = apply(pj, "uat", approve=forged, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "refused", res.get("status")


def test_reviewed_token_cannot_approve_tampered_plan(root, tmp_path, logdir):
    """ADVERSARY: baseline - the originally reviewed token does not approve an edited plan."""
    pj = _safe_plan(root, tmp_path)
    plan = json.loads(pj.read_text(encoding="utf-8"))
    reviewed = plan["approval_token"]
    plan["items"][0]["change"].append("SELECT 1")
    pj.write_text(json.dumps(plan), encoding="utf-8")
    assert apply(pj, "uat", approve=reviewed, execute=True, root=root, log_dir=logdir)["status"] == "refused"


def test_execute_takes_backup_before_change(root, tmp_path, logdir):
    """ADVERSARY: baseline - an executed apply records a backup dir whose register matches the pre-change files."""
    ws = workspaces(root)["uat"]
    before = {k: v for k, v in _tree(ws.path).items()}
    pj = _safe_plan(root, tmp_path)
    tok = json.loads(pj.read_text(encoding="utf-8"))["approval_token"]
    res = apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "applied"
    reg = json.loads((Path(res["backup_dir"]) / "backup-register.json").read_text(encoding="utf-8"))
    for rel, h in reg["files"].items():
        assert before[rel] == h


def test_rollback_restores_exactly_including_files_created(root, tmp_path, logdir):
    """ADVERSARY: UAT has no databricks.yml. The plan deploys one plus a view that fails. After the automatic
    rollback the workspace must be byte-for-byte what it was - but restore only copies registered files back
    and never removes the newly created bundle."""
    ws = workspaces(root)["uat"]
    ws.bundle_path.unlink()
    before = _tree(ws.path)
    plan = build_plan(root, "dev", "uat", kinds=("bundle",))
    assert plan["items"], "expected a bundle item"
    plan["items"].append({"id": "999", "kind": "view", "object": "core.v_adv_bad", "action": "create_view",
                          "change": ["CREATE VIEW core.v_adv_bad AS SELECT CAST('not a number' AS INTEGER) AS v"],
                          "rollback": ["DROP VIEW IF EXISTS core.v_adv_bad"],
                          "backup": {"type": "ddl_capture"}, "target_fingerprint": None,
                          "expected_fingerprint": None})
    pj, _ = write_plan(plan, lint(plan, root), tmp_path / "plans")
    res = apply(pj, "uat", approve=plan_token(plan), execute=True, root=root, log_dir=logdir)
    assert res["status"] == "rolled_back"
    assert not ws.bundle_path.exists(), "bundle created by the failed change survived the rollback"
    assert _tree(ws.path) == before


def test_rollback_restores_db_and_jobs_exactly(root, tmp_path, logdir):
    """ADVERSARY: a failing apply that created jobs and changed config is restored byte-for-byte."""
    ws = workspaces(root)["uat"]
    (ws.jobs_dir / "fx_load.json").unlink()  # forces a job create item
    before = _tree(ws.path)
    plan = build_plan(root, "dev", "uat", kinds=("config_table", "job"))
    plan["items"].append({"id": "999", "kind": "view", "object": "core.v_adv_bad", "action": "create_view",
                          "change": ["CREATE VIEW core.v_adv_bad AS SELECT CAST('not a number' AS INTEGER) AS v"],
                          "rollback": ["DROP VIEW IF EXISTS core.v_adv_bad"],
                          "backup": {"type": "ddl_capture"}, "target_fingerprint": None,
                          "expected_fingerprint": None})
    pj, _ = write_plan(plan, lint(plan, root), tmp_path / "plans")
    f = lint(plan, root)
    acc = tuple(sorted({x["rule"] for x in f if x["severity"] == "HIGH"}))
    res = apply(pj, "uat", approve=plan_token(plan), execute=True, accept_risks=acc, root=root, log_dir=logdir)
    assert res["status"] == "rolled_back"
    assert _tree(ws.path) == before


def test_promotion_does_not_silently_clobber_prod_hotfix(root):
    """ADVERSARY: PROD holds a hotfix (v_large_claims threshold 25000) that exists in no lower env. The UAT->PROD
    plan overwrites it; the linter must raise a dedicated 'overwrites a PROD-only change' finding, not only the
    generic R1 job-schedule warning."""
    plan = build_plan(root, "uat", "prod")
    it = next(i for i in plan["items"] if i["object"] == "core.v_large_claims")
    rules = {x["rule"] for x in lint(plan, root) if x["item"] == it["id"]}
    assert rules - {"R1"}, f"only {rules} raised for a PROD hotfix overwrite"


def test_accept_risk_is_per_item_not_global(root, tmp_path, logdir):
    """ADVERSARY: '--accept-risk R1' accepts every R1 item in the plan, including one added after review.
    Acceptance must name the item(s) reviewed; a bare rule name should not accept an R1 item the human did not
    see. Encoded: accepting 'R1:<id>' for one item still refuses when a second R1 item is present."""
    plan = build_plan(root, "uat", "prod")
    f = lint(plan, root)
    r1 = sorted({x["item"] for x in f if x["rule"] == "R1"})
    assert len(r1) >= 2
    _, b = split(plan, f)
    pj, _ = write_plan(b, lint(b, root), tmp_path / "plans")
    res = apply(pj, "prod", approve=plan_token(b), execute=False, accept_risks=(f"R1:{r1[0]}",),
                root=root, log_dir=logdir)
    assert res["status"] == "refused"
    res2 = apply(pj, "prod", approve=plan_token(b), execute=False, accept_risks=("R1",), root=root, log_dir=logdir)
    assert res2["status"] == "refused", "a bare 'R1' blanket-accepted every R1 item"
