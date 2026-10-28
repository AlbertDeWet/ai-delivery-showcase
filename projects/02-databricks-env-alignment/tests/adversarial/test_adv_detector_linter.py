"""Adversarial tests (role: ADVERSARY) - fast, no seeded workspace needed.

These encode CORRECT behaviour. A failing test here is a finding against the builder's code.
"""
import json
import re
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from envalign.envdiff import Diff, compare  # noqa: E402
from envalign.normalise import norm_job, norm_sql  # noqa: E402
from envalign.promote import _cfg_items, lint  # noqa: E402


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------
def _view_snap(env, views):
    return {"env": env, "view": {k: {"sql": v, "norm": norm_sql(v, mask_env=False), "norm_env": norm_sql(v)}
                                 for k, v in views.items()},
            "config_table": {}, "job": {}, "bundle": {}}


def _job_snap(env, jobs):
    return {"env": env, "view": {}, "config_table": {}, "bundle": {},
            "job": {k: {"raw": j, "norm": norm_job(j, "identical"), "norm_env": norm_job(j, "expected")}
                    for k, j in jobs.items()}}


def _status(diffs, obj):
    return next(d.status for d in diffs if d.obj == obj)


def _empty_target(tmp_path, env="uat", jobs=None):
    root = tmp_path / "ws"
    (root / env / "jobs").mkdir(parents=True)
    for name, j in (jobs or {}).items():
        (root / env / "jobs" / f"{name}.json").write_text(json.dumps(j), encoding="utf-8")
    return root


def _item(obj, change, kind="view", **kw):
    it = {"id": "001", "kind": kind, "object": obj, "action": "replace_view", "change": change,
          "rollback": ["CREATE OR REPLACE VIEW x AS SELECT 1"],
          "backup": {"type": "ddl_capture", "object": obj}}
    it.update(kw)
    return it


def _plan(items, target="uat"):
    return {"plan_id": "adv", "source": "dev", "target": target, "items": items}


def _job(name, uses=None, schedule=True, paused="UNPAUSED", continuous=False, tags=True):
    j = {"name": name, "tasks": [{"task_key": "t", "notebook_task": {"notebook_path": "/x"}}]}
    if schedule:
        j["schedule"] = {"quartz_cron_expression": "0 0/30 * * * ?", "pause_status": paused}
    if continuous:
        j["continuous"] = {"pause_status": "UNPAUSED"}
    if tags:
        j["tags"] = {"owner": "reporting", "uses_objects": ",".join(uses or [])}
    return j


# ----------------------------------------------------------------------------------------------
# Detector: cosmetic changes must be identical (baseline - these should PASS)
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("variant", [
    "select  *\n\tfrom core.claim   where amount >= 50000;",
    "-- header comment\nSELECT * FROM core.claim WHERE amount >= 50000 -- trailing",
    "SELECT /* why */ * FROM core.claim WHERE amount>=50000",
    "CREATE OR REPLACE VIEW core.v_large_claims AS SELECT * FROM core.claim WHERE amount >= 50000",
])
def test_cosmetic_ddl_change_is_identical(variant):
    """ADVERSARY: whitespace / keyword case / comment-only / CREATE-prefix changes are NOT drift."""
    base = "SELECT * FROM core.claim WHERE amount >= 50000"
    assert norm_sql(variant) == norm_sql(base)


def test_comment_markers_inside_string_literal_are_kept():
    """ADVERSARY: '--' inside a string literal is data, not a comment; changing it after the marker is drift."""
    a = "SELECT * FROM t WHERE note = 'a -- b'"
    b = "SELECT * FROM t WHERE note = 'a -- c'"
    assert norm_sql(a) != norm_sql(b)


# ----------------------------------------------------------------------------------------------
# Detector: real semantic changes wrongly normalised away (should FAIL today)
# ----------------------------------------------------------------------------------------------
def test_double_dash_inside_quoted_identifier_does_not_hide_drift():
    """ADVERSARY: '--' inside a double-quoted identifier is treated as a comment, so everything after it on the
    line (other columns, filters) is ignored and real drift is reported as identical."""
    a = 'SELECT premium AS "net--gross", premium * 0.15 AS tax FROM core.policy'
    b = 'SELECT premium AS "net--gross", premium * 0.99 AS tax FROM core.policy WHERE 1=0'
    assert norm_sql(a) != norm_sql(b)


def test_quoted_identifier_case_is_semantic():
    """ADVERSARY: the output column name "Amount" vs "amount" is a contract change for consumers; lower-casing
    inside double quotes hides it."""
    assert norm_sql('SELECT x AS "Amount" FROM t') != norm_sql('SELECT x AS "amount" FROM t')


def test_quoted_identifier_whitespace_is_semantic():
    """ADVERSARY: "Net  Amount" and "Net Amount" are different column names; whitespace collapsing hides it."""
    assert norm_sql('SELECT x AS "Net  Amount" FROM t') != norm_sql('SELECT x AS "Net Amount" FROM t')


def test_prod_view_pointing_at_dev_catalog_is_drift():
    """ADVERSARY: a PROD view that reads contoso_dev (cross-environment leak) is reported identical to DEV,
    because the identical-level comparison never asks whether env tokens belong to the target env."""
    sql = "SELECT * FROM contoso_dev.core.claim"
    diffs = compare(_view_snap("dev", {"core.v": sql}), _view_snap("prod", {"core.v": sql}))
    assert _status(diffs, "core.v") not in ("identical", "expected")


def test_dev_view_pointing_at_uat_catalog_is_not_expected():
    """ADVERSARY: DEV reading contoso_uat while UAT reads contoso_uat is masked to 'expected per-env' - it is a
    real wrong-environment reference."""
    sql = "SELECT * FROM contoso_uat.core.claim"
    diffs = compare(_view_snap("dev", {"core.v": sql}), _view_snap("uat", {"core.v": sql}))
    assert _status(diffs, "core.v") == "drift"


def test_prod_job_paused_is_drift_not_expected():
    """ADVERSARY: a PROD scheduled job that is PAUSED (while UAT runs it) silently stops production; the
    allow-list masks pause_status in every direction and calls it an expected env difference."""
    uat = _job("claims_daily", ["core.v_large_claims"], paused="UNPAUSED")
    prod = _job("claims_daily", ["core.v_large_claims"], paused="PAUSED")
    diffs = compare(_job_snap("uat", {"claims_daily": uat}), _job_snap("prod", {"claims_daily": prod}))
    assert _status(diffs, "claims_daily") == "drift"


def test_prod_job_without_failure_alerts_is_drift():
    """ADVERSARY: removing on_failure alerts in PROD is masked as 'expected' because the whole
    email_notifications block is allow-listed."""
    uat = _job("fx_load", ["core.cfg_fx_source"])
    uat["email_notifications"] = {"on_failure": ["oncall-uat@contoso-insurance.example"]}
    prod = _job("fx_load", ["core.cfg_fx_source"])
    prod["email_notifications"] = {}
    diffs = compare(_job_snap("uat", {"fx_load": uat}), _job_snap("prod", {"fx_load": prod}))
    assert _status(diffs, "fx_load") == "drift"


def test_cfg_null_vs_string_none_produces_a_change():
    """ADVERSARY: the detector sees NULL vs 'None' as drift (row_hash keeps None), but the planner compares
    str() values so it emits NO change: the plan never converges and T2 fails forever."""
    cols = [["key", "VARCHAR"], ["value", "VARCHAR"], ["active", "BOOLEAN"]]
    src = {"_cfg_raw": {"core.cfg_x": {"columns": cols, "rows": [{"key": "k1", "value": None, "active": True}]}}}
    tgt = {"_cfg_raw": {"core.cfg_x": {"columns": cols, "rows": [{"key": "k1", "value": "None", "active": True}]}}}
    items = _cfg_items([Diff("config_table", "core.cfg_x", "drift")], src, tgt, "tag")
    assert items and items[0]["change"], "drifted row produced no SQL change"


# ----------------------------------------------------------------------------------------------
# Linter R1: indirect use, untagged and continuous jobs
# ----------------------------------------------------------------------------------------------
def test_r1_baseline_direct_use_is_flagged(tmp_path):
    """ADVERSARY: baseline - CREATE OR REPLACE on a view a scheduled job tags directly IS flagged R1."""
    root = _empty_target(tmp_path, jobs={"refresh": _job("refresh", ["core.v_inner"])})
    f = lint(_plan([_item("core.v_inner", ["CREATE OR REPLACE VIEW core.v_inner AS SELECT 2"])]), root)
    assert "R1" in [x["rule"] for x in f]


def test_r1_nested_view_dependency_flagged(tmp_path):
    """ADVERSARY: the job uses core.v_outer which SELECTs FROM core.v_inner. Replacing v_inner breaks the job,
    but R1 only matches the tag literally."""
    root = _empty_target(tmp_path, jobs={"refresh": _job("refresh", ["core.v_outer"])})
    plan = _plan([_item("core.v_inner", ["CREATE OR REPLACE VIEW core.v_inner AS SELECT 2 AS x"]),
                  _item("core.v_outer", ["CREATE VIEW IF NOT EXISTS core.v_outer AS SELECT x FROM core.v_inner"],
                        id="002", action="create_view")])
    plan["items"][1]["id"] = "002"
    f = lint(plan, root)
    assert any(x["rule"] == "R1" and x["item"] == "001" for x in f)


def test_r1_untagged_scheduled_job_is_not_silently_trusted(tmp_path):
    """ADVERSARY: an unpaused scheduled job with no uses_objects tag may read anything; a CREATE OR REPLACE
    plan must at least raise a finding that dependencies are unknown. Today: zero findings."""
    root = _empty_target(tmp_path, jobs={"adhoc": _job("adhoc", tags=False)})
    f = lint(_plan([_item("core.v_large_claims", ["CREATE OR REPLACE VIEW core.v_large_claims AS SELECT 1"])]), root)
    assert f, "no finding although an untagged running job exists"


def test_r1_continuous_job_counts_as_running(tmp_path):
    """ADVERSARY: a continuous (streaming) job has no quartz schedule, so R1 ignores it even though it is
    always running and tags the replaced view."""
    root = _empty_target(tmp_path, jobs={"stream": _job("stream", ["core.v_large_claims"], schedule=False,
                                                        continuous=True)})
    f = lint(_plan([_item("core.v_large_claims", ["CREATE OR REPLACE VIEW core.v_large_claims AS SELECT 1"])]), root)
    assert "R1" in [x["rule"] for x in f]


def test_r1_drop_table_on_used_config_table(tmp_path):
    """ADVERSARY: baseline - a rebuild (DROP TABLE) of a config table a running job uses is flagged R1."""
    root = _empty_target(tmp_path, jobs={"fx": _job("fx", ["core.cfg_fx_source"])})
    it = _item("core.cfg_fx_source", ["DROP TABLE core.cfg_fx_source", "CREATE TABLE core.cfg_fx_source(key VARCHAR)"],
               kind="config_table", action="rebuild_table")
    assert "R1" in [x["rule"] for x in lint(_plan([it]), root)]


# ----------------------------------------------------------------------------------------------
# Linter R2: protected namespace via aliases
# ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("stmt", [
    "DELETE FROM team_b.ingest_log",            # baseline, plain
    'DELETE FROM "team_b".ingest_log',          # quoted identifier
    'DELETE FROM "team_b"."ingest_log"',
    "DELETE FROM `team_b`.ingest_log",          # Databricks backticks
    "USE team_b; DELETE FROM ingest_log",       # schema switch
    "SET schema = 'team_b'",
])
def test_r2_protected_namespace_aliases(tmp_path, stmt):
    """ADVERSARY: any statement touching team_b - quoted, backticked or via USE/SET schema - is blocking R2."""
    root = _empty_target(tmp_path)
    f = lint(_plan([_item("core.v_x", [stmt])]), root)
    assert any(x["rule"] == "R2" and x["blocking"] for x in f), stmt


def test_r2_job_owner_tag_case_variant(tmp_path):
    """ADVERSARY: a job payload owned by 'Team_B' (case variant) is the protected team's job."""
    root = _empty_target(tmp_path)
    it = {"id": "001", "kind": "job", "object": "nightly_sync", "action": "deploy_job", "change": ["write"],
          "rollback": ["restore"], "backup": {"type": "file_copy"},
          "payload": {"name": "nightly_sync", "tags": {"owner": "Team_B"}}}
    assert any(x["rule"] == "R2" for x in lint(_plan([it]), root))


# ----------------------------------------------------------------------------------------------
# Linter R3: a backup that is not a backup
# ----------------------------------------------------------------------------------------------
def test_r3_none_needed_backup_on_existing_table_is_blocking(tmp_path):
    """ADVERSARY: an upsert/delete on an EXISTING table whose backup says type 'none_needed' passes R3,
    because R3 only checks that a backup 'type' string exists."""
    root = _empty_target(tmp_path)
    it = _item("core.cfg_report_map", ["DELETE FROM core.cfg_report_map WHERE key = 'a'"], kind="config_table",
               action="upsert_rows", backup={"type": "none_needed"},
               rollback=["DROP TABLE IF EXISTS core.cfg_report_map"])
    assert any(x["rule"] == "R3" and x["blocking"] for x in lint(_plan([it]), root))


def test_r3_rollback_that_only_drops_is_blocking(tmp_path):
    """ADVERSARY: a rollback of 'DROP TABLE' on an existing table destroys data instead of restoring it."""
    root = _empty_target(tmp_path)
    it = _item("core.cfg_report_map", ["DELETE FROM core.cfg_report_map"], kind="config_table",
               action="upsert_rows", backup={"type": "backup_table", "table": "_backup.x", "sql": "SELECT 1"},
               rollback=["DROP TABLE IF EXISTS core.cfg_report_map"])
    assert any(x["rule"] == "R3" for x in lint(_plan([it]), root))


# ----------------------------------------------------------------------------------------------
# README numbers must come from results/
# ----------------------------------------------------------------------------------------------
def test_readme_results_table_matches_results():
    """ADVERSARY: every results-table row in the README appears verbatim in results/demo-summary.md."""
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    summary = (PROJECT / "results" / "demo-summary.md").read_text(encoding="utf-8")
    rows = [ln.strip() for ln in readme.splitlines() if re.match(r"\|\s*(dev|uat)->", ln.strip())]
    assert rows
    for r in rows:
        assert r in summary, r


def test_readme_builder_test_count_matches_results():
    """ADVERSARY: the README builder-test count equals the recorded pytest run."""
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    rec = (PROJECT / "results" / "builder-tests.txt").read_text(encoding="utf-8")
    n_readme = re.search(r"Builder tests: (\d+) passed", readme).group(1)
    assert f"{n_readme} passed" in rec


def test_readme_runtime_claims_are_recorded():
    """ADVERSARY: the README says 'the demo took about 2.5 minutes'; no file in results/ records a demo
    duration, so the number is unbacked (brief rule 4)."""
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    if "2.5 minutes" not in readme:
        return
    blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in (PROJECT / "results").glob("demo-*"))
    assert re.search(r"(duration|elapsed|runtime|took)[^\n]{0,40}\d", blob, re.I), "demo duration not recorded"


def test_readme_uat_prod_pr_sizes_match_results():
    """ADVERSARY: '15 safe items, then 3 R1 items' matches the recorded promotions."""
    summary = (PROJECT / "results" / "demo-summary.md").read_text(encoding="utf-8")
    assert re.search(r"uat-to-prod-\w+-a\.json` items=15", summary)
    # Orchestrator ruling (Round 2): R5 flags on these items are correct (F4); require 3 items and 3 R1 flags.
    m = re.search(r"uat-to-prod-\w+-b\.json` items=3 lint=\[([^\]]*)\]", summary)
    assert m, "PR b with 3 items not recorded"
    rules = re.findall(r"'(R\d)'", m.group(1))
    assert rules.count("R1") == 3
    assert set(rules) <= {"R1", "R5"}
