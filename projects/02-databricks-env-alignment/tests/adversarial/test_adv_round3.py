"""Adversarial tests, Round 3 (role: ADVERSARY). Attacks the HMAC ledger, lint-input binding, the dynamic-SQL
blocklist, protected-team discovery, ROLLBACK_PARTIAL exit codes and README numbers.

These encode CORRECT behaviour; a failing test is an open finding. Kept fast: most avoid the seeded estate.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import envalign.approvals as approvals  # noqa: E402
from envalign.apply import apply  # noqa: E402
from envalign.promote import build_plan, lint, plan_token, split, write_plan  # noqa: E402


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
@pytest.fixture
def secret(tmp_path, monkeypatch):
    """Never touch the project's real secret: point the ledger at a per-test key."""
    p = tmp_path / "secret" / "secret.key"
    monkeypatch.setattr(approvals, "SECRET_PATH", p)
    return p


def _issued(root, tmp_path, kinds=("config_table",)):
    plan = build_plan(root, "dev", "uat", kinds=kinds)
    a, _ = split(plan, lint(plan, root))
    pj, _ = write_plan(a, lint(a, root), tmp_path / "plans")
    return pj, json.loads(pj.read_text(encoding="utf-8"))["approval_token"]


def _empty_target(tmp_path, jobs):
    root = tmp_path / "ws"
    (root / "uat" / "jobs").mkdir(parents=True)
    for name, j in jobs.items():
        (root / "uat" / "jobs" / f"{name}.json").write_text(json.dumps(j), encoding="utf-8")
    return root


def _view_item(obj, stmt, action="create_view"):
    return {"id": "001", "kind": "view", "object": obj, "action": action, "change": [stmt],
            "rollback": [f"DROP VIEW IF EXISTS {obj}"], "backup": {"type": "ddl_capture", "object": obj}}


def _plan(items):
    return {"plan_id": "adv3", "source": "dev", "target": "uat", "items": items}


# ----------------------------------------------------------------------------------------------
# Ledger
# ----------------------------------------------------------------------------------------------
def test_ledger_baseline_genuine_plan_dry_runs(root, tmp_path, logdir, secret):
    """ADVERSARY: baseline - an issued plan passes the ledger in a dry run."""
    pj, tok = _issued(root, tmp_path)
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "dry-run"


def test_ledger_entry_for_other_environment_refused(root, tmp_path, logdir, secret):
    """ADVERSARY: retarget an issued UAT plan to PROD (rewriting target and stored token) - refused."""
    pj, _ = _issued(root, tmp_path)
    p = json.loads(pj.read_text(encoding="utf-8"))
    p["target"] = "prod"
    p["approval_token"] = plan_token(p)
    pj.write_text(json.dumps(p), encoding="utf-8")
    assert apply(pj, "prod", approve=p["approval_token"], root=root, log_dir=logdir)["status"] == "refused"


def test_ledger_line_deleted_refused_and_reorder_harmless(root, tmp_path, logdir, secret):
    """ADVERSARY: deleting the entry refuses; reordering / duplicating lines changes nothing."""
    pj, tok = _issued(root, tmp_path)
    lp = approvals.ledger_path(root)
    lines = lp.read_text(encoding="utf-8").splitlines()
    lp.write_text("\n".join(reversed(lines + lines)) + "\n", encoding="utf-8")
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "dry-run"
    lp.write_text("", encoding="utf-8")
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "refused"


def test_ledger_secret_regenerated_mid_flow_refused(root, tmp_path, logdir, secret):
    """ADVERSARY: the secret is replaced between plan and apply - old entries no longer verify."""
    pj, tok = _issued(root, tmp_path)
    secret.write_bytes(b"x" * 32)
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "refused"


def test_ledger_missing_secret_is_not_silently_recreated_by_apply(root, tmp_path, logdir, secret):
    """ADVERSARY: with the secret deleted, apply refuses - but verify() quietly GENERATES a new secret as a
    side effect. A verifier must never mint key material; the missing key should be reported as such."""
    pj, tok = _issued(root, tmp_path)
    secret.unlink()
    res = apply(pj, "uat", approve=tok, root=root, log_dir=logdir)
    assert res["status"] == "refused"
    assert not secret.exists(), "apply/verify created a fresh HMAC secret"
    assert "secret" in res.get("reason", "").lower()


def test_ledger_replay_after_apply_and_reset_refused(root, tmp_path, logdir, secret):
    """ADVERSARY: an approval is single-use. After it is executed and the estate is later reset to the old state
    (restore, re-seed, a rollback by hand), the same old token must not execute again weeks later."""
    pj, tok = _issued(root, tmp_path)
    ws_dir = Path(root) / "uat"
    saved = tmp_path / "uat-saved"
    shutil.copytree(ws_dir, saved)
    assert apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)["status"] == "applied"
    shutil.rmtree(ws_dir)
    shutil.copytree(saved, ws_dir)
    res = apply(pj, "uat", approve=tok, execute=True, root=root, log_dir=logdir)
    assert res["status"] == "refused", "a used approval was replayed"


@pytest.mark.xfail(strict=True, reason="H4 documented limitation: the ledger proves issuance, not human review; "
                                        "write_plan signs any plan dict (README limitations)")
def test_ledger_reissuing_an_edited_plan_is_not_approval(root, tmp_path, logdir, secret):
    """ADVERSARY: write_plan() signs ANY plan dict it is handed, so an edited plan passed back through write_plan
    gets a genuine MAC. The ledger proves issuance, not that the content came from build_plan or a reviewer
    (documented in README limitations -> MEDIUM)."""
    plan = build_plan(root, "dev", "uat", kinds=("config_table",))
    a, _ = split(plan, lint(plan, root))
    cfg = a["items"][0]
    cfg["change"].append(f"DELETE FROM {cfg['object']}")  # allowed shape (Round 5 ruling): wipes the table
    pj, _ = write_plan(a, lint(a, root), tmp_path / "plans")
    tok = json.loads(pj.read_text(encoding="utf-8"))["approval_token"]
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "refused"


# ----------------------------------------------------------------------------------------------
# Lint-input hash coverage
# ----------------------------------------------------------------------------------------------
def test_change_to_planned_config_table_after_approval_refused(root, tmp_path, logdir, secret):
    """ADVERSARY: baseline - a config table in the plan edited after approval is refused (stale guard)."""
    import duckdb
    pj, tok = _issued(root, tmp_path)
    obj = json.loads(pj.read_text(encoding="utf-8"))["items"][0]["object"]
    con = duckdb.connect(str(Path(root) / "uat" / "catalog.duckdb"))
    con.execute(f"INSERT INTO {obj} VALUES ('adv3_key', 'x', TRUE)")
    con.close()
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "refused"


def test_new_team_b_job_after_approval_refused(root, tmp_path, logdir, secret):
    """ADVERSARY: baseline - adding a protected-team job that reads a planned object after approval refuses."""
    pj, tok = _issued(root, tmp_path)
    obj = json.loads(pj.read_text(encoding="utf-8"))["items"][0]["object"]
    (Path(root) / "uat" / "jobs" / "team_b_new.json").write_text(json.dumps(
        {"name": "team_b_new", "tags": {"owner": "team_b", "uses_objects": obj}}), encoding="utf-8")
    assert apply(pj, "uat", approve=tok, root=root, log_dir=logdir)["status"] == "refused"


# ----------------------------------------------------------------------------------------------
# Dynamic-SQL blocklist
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("stmt", [
    "ExEcUtE\n   ImMeDiAtE 'SELECT 1'",                              # case + newline (baseline)
    "SELECT * FROM main.query('SELECT 1')",                          # qualified (baseline)
    "EXECUTE/**/IMMEDIATE 'DELETE FROM team' || '_b.ingest_log'",   # comment splitting
    "EXECUTE -- x\nIMMEDIATE 'DELETE FROM team' || '_b.ingest_log'",
    "SELECT * FROM \"query\"('SELECT * FROM team' || '_b.ingest_log')",  # quoted function name
    "SELECT * FROM query/**/('SELECT * FROM team' || '_b.ingest_log')",  # comment before paren (runs in DuckDB)
    "SELECT * FROM json_execute_serialized_sql('{\"x\":1}')",       # other DuckDB SQL-executing function
])
def test_dynamic_sql_variants_are_blocking(tmp_path, stmt):
    """ADVERSARY: every way of spelling dynamic SQL is blocking R2: comments between keywords, a quoted function
    name, and DuckDB's other SQL-executing function json_execute_serialized_sql."""
    f = lint(_plan([_view_item("core.v_x", stmt)]), _empty_target(tmp_path, {}))
    assert any(x["rule"] == "R2" and x["blocking"] for x in f), stmt


# ----------------------------------------------------------------------------------------------
# Protected-team discovery through untagged jobs
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("job", [
    {"name": "team_b_ingest", "tags": {"owner": "team_b"},
     "schedule": {"quartz_cron_expression": "0 15 * * * ?", "pause_status": "PAUSED"}},
    {"name": "nightly_recon", "tags": {"owner": "team_b"}},
])
def test_untagged_protected_job_makes_changes_blocking(tmp_path, job):
    """ADVERSARY: the protected team has a job with no uses_objects tag (paused or not scheduled). Its
    dependencies are unknown, so ANY replace could change their results; the linter must not treat
    'unknown' as 'nothing'. Today: R1 ignores paused jobs and R2 only uses tagged objects -> no finding."""
    root = _empty_target(tmp_path, {job["name"]: job})
    it = _view_item("core.v_large_claims", "CREATE OR REPLACE VIEW core.v_large_claims AS SELECT 1",
                    action="replace_view")
    it["backup"]["previous_sql"] = "SELECT 2"
    it["rollback"] = ["CREATE OR REPLACE VIEW core.v_large_claims AS SELECT 2"]
    f = lint(_plan([it]), root)
    assert any(x["rule"] == "R2" for x in f), [x["rule"] for x in f]


# ----------------------------------------------------------------------------------------------
# ROLLBACK_PARTIAL exit codes
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,code", [("ROLLBACK_PARTIAL", 3), ("rolled_back", 1), ("refused", 1),
                                         ("applied", 0), ("dry-run", 0)])
def test_cli_exit_codes(monkeypatch, status, code):
    """ADVERSARY: `python -m envalign apply` exits 3 on ROLLBACK_PARTIAL, non-zero on any failure."""
    import envalign.__main__ as cli
    import envalign.apply as ap
    monkeypatch.setattr(ap, "apply", lambda *a, **k: {"status": status})
    assert cli.main(["apply", "--plan", "x.json", "--env", "uat"]) == code


def test_demo_exit_code_reflects_partial_rollback(monkeypatch):
    """ADVERSARY: the one-command demo must not exit 0 if any apply ends ROLLBACK_PARTIAL."""
    import envalign.demo as demo
    src = Path(demo.__file__).read_text(encoding="utf-8")
    assert "ROLLBACK_PARTIAL" in src or "status" in src and re.search(r"return\s+[1-9]", src), \
        "demo never inspects apply status for its exit code"


# ----------------------------------------------------------------------------------------------
# README numbers vs results/
# ----------------------------------------------------------------------------------------------
def test_readme_test_counts_match_results():
    """ADVERSARY: README 'N passed' claims for builder and adversary suites equal the recorded runs."""
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    b = re.search(r"Builder tests: (\d+) passed", readme).group(1)
    a = re.search(r"Adversary tests: (\d+) passed", readme).group(1)
    assert re.search(rf"\b{b} passed", (PROJECT / "results" / "builder-tests.txt").read_text(encoding="utf-8"))
    assert re.search(rf"\b{a} passed", (PROJECT / "results" / "adversarial-tests.txt").read_text(encoding="utf-8"))


def test_readme_demo_elapsed_matches_results():
    """ADVERSARY: the README demo time equals the recorded demo_elapsed_seconds."""
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    s = re.search(r"Demo elapsed time: ([\d.]+) s", readme).group(1)
    js = json.loads((PROJECT / "results" / "demo-summary.json").read_text(encoding="utf-8"))
    blob = json.dumps(js)
    assert re.search(rf"\b{re.escape(s)}\b", blob), f"{s} not in demo-summary.json"


def test_readme_round_failure_counts_match_log():
    """ADVERSARY: README round counts ('25 of 39', '15 of 60') match ADVERSARIAL-LOG.md."""
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    log = (PROJECT / "ADVERSARIAL-LOG.md").read_text(encoding="utf-8")
    assert "25 of 39" in readme and "25 failed, 14 passed" in log
    assert "15 of 60" in readme and "15 failed, 45 passed" in log
