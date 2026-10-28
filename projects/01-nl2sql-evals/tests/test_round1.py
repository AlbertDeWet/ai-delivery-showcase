"""Builder tests for the adversarial round 1 fixes (A1-A5, E1/E2, I1/I2, G1-G3, regrade)."""

import json
import subprocess

import pytest

from nl2sql import evals, guard, pipeline
from nl2sql.providers.base import INFRA_PREFIX, Generation
from nl2sql.providers.claude_cli import ClaudeCliProvider


def rec(cid, correct, action="sql", error="", in_tok=0, out_tok=0, repeat=0, sql=""):
    return {"id": cid, "category": "simple", "repeat": repeat, "expect": ["answer"], "question": "q",
            "action": action, "correct": correct, "reason": "", "sql": sql, "guard_reasons": [], "error": error,
            "latency_s": 1.0, "input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": None, "retries": 0,
            "first_error": "", "check": {"status": "abstain", "key_number": None, "control": None, "note": "",
                                         "input_tokens": 0, "output_tokens": 0}}


def run(records, repeats=3, label=None, attempt=None, ts="2026-10-23T00:00:00+00:00", rule=None):
    r = {"provider": "claude-cli", "tier": "sonnet", "model": "claude-sonnet-5-5", "suite": "questions",
         "repeats": repeats, "n_cases": len({x["id"] for x in records}),
         "case_ids": sorted({x["id"] for x in records}), "label": label, "timestamp": ts,
         "summary": evals.summarize(records, repeats), "records": records}
    if attempt:
        r["attempt"] = attempt
    if rule:
        r["retry_rule"] = rule
    return r


def three(cid, ok):
    return [rec(cid, ok, repeat=k) for k in range(3)]


BASE = run([r for c in ("s01", "s03") for r in three(c, c == "s01")])


# ---- A1: versioned re-runs, attempt accounting ----

def test_save_numbers_attempts_and_never_overwrites(tmp_path):
    p1 = evals.save(run(three("s03", False), label="x"), tmp_path)
    p2 = evals.save(run(three("s03", True), label="x"), tmp_path)
    assert p1.name.endswith("-x-attempt1.json") and p2.name.endswith("-x-attempt2.json")
    assert json.loads(p1.read_text())["records"][0]["correct"] is False  # first attempt kept intact


def test_legacy_unnumbered_rerun_counts_as_attempt_one(tmp_path):
    legacy = run(three("s03", True), label="x")
    (tmp_path / "reruns").mkdir()
    (tmp_path / "reruns" / "claude-cli-sonnet-x.json").write_text(json.dumps(legacy))
    assert evals.save(run(three("s03", True), label="x"), tmp_path).name.endswith("-attempt2.json")


def test_merge_refuses_when_attempts_are_left_out():
    a2 = run(three("s03", True), label="x", attempt=2)
    with pytest.raises(ValueError, match="report every attempt"):
        evals.merge_results(BASE, [a2], rerun_names=["a2.json"], attempts_on_disk={"claude-cli-sonnet-x": 2})


def test_merge_uses_earliest_attempt_and_records_all():
    a1 = run(three("s03", False), label="x", attempt=1, ts="2026-10-23T01:00:00+00:00")
    a2 = run(three("s03", True), label="x", attempt=2, ts="2026-10-23T02:00:00+00:00")
    out = evals.merge_results(BASE, [a2, a1], rerun_names=["a2.json", "a1.json"],
                              attempts_on_disk={"claude-cli-sonnet-x": 2})
    series = out["derived_from"]["series"][0]
    assert series["attempts_on_disk"] == 2 and series["attempts_reported"] == 2
    assert series["used"] == {"s03": "a1.json"}
    assert [a["correct"] for a in series["attempts"]] == [0, 3]
    assert not any(r["correct"] for r in out["records"] if r["id"] == "s03")  # the later pass is not used


def test_cli_merge_refuses_unreported_attempt(tmp_path):
    base_p = tmp_path / "base.json"
    base_p.write_text(json.dumps(BASE))
    p1 = evals.save(run(three("s03", False), label="x"), tmp_path)
    p2 = evals.save(run(three("s03", True), label="x"), tmp_path)
    from nl2sql.cli import main

    assert main(["merge", "--base", str(base_p), "--rerun", str(p2), "--out", str(tmp_path / "d.json"),
                 "--results-dir", str(tmp_path)]) == 2
    assert main(["merge", "--base", str(base_p), "--rerun", str(p1), str(p2), "--out", str(tmp_path / "d.json"),
                 "--results-dir", str(tmp_path)]) == 0


# ---- A2-A5 ----

def test_merge_rejects_wrong_repeat_indices():
    bad = run([rec("s03", True, repeat=0), rec("s03", True, repeat=0), rec("s03", True, repeat=1)])
    with pytest.raises(ValueError, match="exactly 3 records"):
        evals.merge_results(BASE, [bad])


def test_merge_states_mixed_retry_rule():
    out = evals.merge_results(BASE, [run(three("s03", True), rule=evals.RETRY_RULE)])
    assert out["retry_rule"].startswith("mixed: base records: none")


# ---- E1/E2: report display ----

def _row(tmp_path, records):
    (tmp_path / "claude-cli-sonnet.json").write_text(json.dumps(run(records, repeats=1)))
    return [l for l in evals.render_report(tmp_path).splitlines() if "claude-sonnet-5-5 | questions" in l][0]


def test_execution_errors_render_as_zero_percent_result(tmp_path):
    row = _row(tmp_path, [rec("s01", False, "error", "execution error: Binder Error", 1800, 30)])
    assert "not run" not in row and "| 0.0% |" in row


def test_partial_infra_errors_are_flagged(tmp_path):
    row = _row(tmp_path, [rec("s01", False, "error", f"{INFRA_PREFIX} timed out"), rec("s02", True)])
    assert "(incl. 1/2 infra errors)" in row


# ---- I1/I2: only explicit transport failures are infra ----

def test_nonzero_exit_without_output_is_not_retried(con):
    calls = []

    def runner(cmd, **kw):
        calls.append(1)
        return subprocess.CompletedProcess(cmd, 1, "", "not logged in")

    pipeline.ask("q", ClaudeCliProvider(runner=runner), con)
    assert len(calls) == 1


def test_timeout_is_infra_and_retried_once(con):
    calls = []

    def runner(cmd, **kw):
        calls.append(1)
        raise subprocess.TimeoutExpired(cmd, 1)

    res = pipeline.ask("q", ClaudeCliProvider(runner=runner), con)
    assert len(calls) == 2 and res.retries == 1 and res.first_error.startswith(INFRA_PREFIX)


def test_zero_tokens_alone_is_not_infra():
    assert not pipeline.is_infra_error(Generation("error", message="unparseable model output: no JSON"))
    assert pipeline.is_infra_error(Generation("error", message="x", infra=True))


# ---- G1-G3: guard ----

def test_cte_scope_inherited_by_nested_query():
    assert guard.check("WITH a AS (SELECT * FROM claims) "
                       "SELECT * FROM (WITH b AS (SELECT * FROM a) SELECT * FROM b) t").ok


def test_cte_not_visible_in_sibling_or_outer_query():
    assert not guard.check("SELECT * FROM (WITH a AS (SELECT 1 AS x) SELECT * FROM a) t, a").ok


def test_cte_name_must_be_plain_identifier():
    assert not guard.check('WITH "my.csv" AS (SELECT 1) SELECT * FROM "my.csv"').ok


def test_quoted_identifier_function_still_blocked_after_g3():
    assert not guard.check("SELECT \"current_setting\"('threads')").ok


# ---- offline regrade ----

def test_regrade_reexecutes_stored_sql_and_reports_changes(con):
    cases = [c for c in evals.load_cases() if c["id"] == "s01"]
    recorded = run([rec("s01", True, sql="SELECT COUNT(*) + 1 FROM policies"),
                    rec("s01", True, action="refuse")], repeats=1)
    out = evals.regrade_result(recorded, cases, con, "x.json", "evals/questions.yaml")
    assert [r["correct"] for r in out["records"]] == [False, True]  # refusal left as recorded
    ch = out["regraded_from"]["changed_records"]
    assert len(ch) == 1 and ch[0]["recorded_correct"] is True and ch[0]["regraded_correct"] is False
    assert out["regraded_from"]["recorded_summary"] == recorded["summary"]
