"""Live-eval review fixes: infra-only retry, all-error runs never shown as 0%, re-run files and merge."""

import json

import pytest

from nl2sql import evals, pipeline
from nl2sql.grader import result_sets_match
from nl2sql.providers.base import Generation


class ScriptedProvider:
    """Returns the scripted generations in order; counts calls."""
    name, model, tier = "claude-cli", "claude-haiku-5-5", "haiku"

    def __init__(self, gens, control=None):
        self.gens = list(gens)
        self.calls = 0
        self.control = control or [Generation("refuse", message="abstain")]

    def generate(self, q, card):
        self.calls += 1
        return self.gens.pop(0)

    def control_total(self, q, card):
        return self.control[0]


TIMEOUT = Generation("error", message="claude CLI failed: timed out after 180 seconds")
GOOD = Generation("sql", sql="SELECT COUNT(*) FROM policies", input_tokens=1800, output_tokens=20)


# ---- infra-only retry rule ----

def test_infra_error_is_retried_once_then_succeeds(con):
    p = ScriptedProvider([TIMEOUT, GOOD])
    res = pipeline.ask("How many policies?", p, con)
    assert res.action == "sql" and res.retries == 1 and "timed out" in res.first_error and p.calls == 2


def test_infra_retry_is_capped_at_one(con):
    p = ScriptedProvider([TIMEOUT, TIMEOUT, GOOD])
    res = pipeline.ask("q", p, con)
    assert res.action == "error" and res.retries == 1 and p.calls == 2


def test_error_with_tokens_is_not_retried(con):
    bad_output = Generation("error", message="unparseable model output", input_tokens=1800, output_tokens=12)
    p = ScriptedProvider([bad_output, GOOD])
    res = pipeline.ask("q", p, con)
    assert res.action == "error" and res.retries == 0 and p.calls == 1


def test_wrong_answer_and_refusal_are_not_retried(con):
    wrong = Generation("sql", sql="SELECT 1", input_tokens=1800, output_tokens=5)
    p = ScriptedProvider([wrong, GOOD])
    assert pipeline.ask("q", p, con).retries == 0 and p.calls == 1
    p = ScriptedProvider([Generation("refuse", message="no"), GOOD])
    assert pipeline.ask("q", p, con).retries == 0 and p.calls == 1


def test_retry_can_be_disabled(con):
    p = ScriptedProvider([TIMEOUT, GOOD])
    assert pipeline.ask("q", p, con, retry_infra=False).action == "error" and p.calls == 1


def test_eval_records_retry_rule_and_count(db_path):
    cases = [c for c in evals.load_cases() if c["id"] == "s01"]
    out = evals.run_eval(ScriptedProvider([TIMEOUT, GOOD]), cases=cases, db_path=db_path, run_checker=False)
    assert out["retry_rule"].startswith("infra-only") and out["summary"]["infra_retries"] == 1
    assert out["records"][0]["retries"] == 1 and out["records"][0]["correct"]


# ---- all-error runs and not-run reasons ----

def _run(tier, model, suite, records, label=None):
    return {"provider": "claude-cli", "tier": tier, "model": model, "suite": suite, "repeats": 1,
            "n_cases": len(records), "case_ids": sorted({r["id"] for r in records}), "label": label,
            "timestamp": "2026-10-23T00:00:00+00:00", "summary": evals.summarize(records, 1),
            "records": records}


def _rec(cid, correct, action="sql", error="", cost=None):
    return {"id": cid, "category": "simple", "repeat": 0, "expect": ["answer"], "question": "q",
            "action": action, "correct": correct, "reason": "", "sql": "", "guard_reasons": [], "error": error,
            "latency_s": 1.0, "input_tokens": 0, "output_tokens": 0, "cost_usd": cost, "retries": 0,
            "first_error": "", "check": {"status": "abstain", "key_number": None, "control": None, "note": "",
                                         "input_tokens": 0, "output_tokens": 0}}


def test_all_error_run_renders_as_not_run_never_zero(tmp_path):
    recs = [_rec("s01", False, "error", "CLI reported error: API Error: 400 version 2.1.280 or newer is required",
                 cost=0.01)]  # the exact form the provider emits (see results/failed-runs/)
    (tmp_path / "claude-cli-opus.json").write_text(json.dumps(_run("opus", "claude-opus-5-5", "questions", recs)))
    report = evals.render_report(tmp_path)
    opus_rows = [l for l in report.splitlines() if "claude-opus-5-5" in l]
    assert opus_rows and all("not run: CLI reported error: API Error: 400" in l for l in opus_rows if "| questions |" in l)
    assert not any("0.0%" in l for l in opus_rows)
    assert "## claude-cli / claude-opus-5-5" not in report  # no failure detail section for a non-run


def test_missing_opus_uses_configured_reason(tmp_path):
    report = evals.render_report(tmp_path)
    assert "| claude-opus-5-5 | questions | - | not run: Claude Code CLI 2.1.276 < 2.1.280 required |" in report
    assert "| claude-opus-5-5 | heldout | - | not run: Claude Code CLI 2.1.276 < 2.1.280 required |" in report


def test_cost_column_says_generation_only(tmp_path):
    assert "generation calls only" in evals.render_report(tmp_path)


def test_quantile_is_nearest_rank():
    assert evals._quantile([1, 2, 3, 4, 100], 0.5) == 3
    assert evals._quantile([1, 2, 3, 4, 100], 0.95) == 100  # index round(0.95 * 4) = 4


# ---- re-run files and merge ----

def test_labelled_rerun_goes_to_reruns_dir(tmp_path):
    r = _run("sonnet", "claude-sonnet-5-5", "questions", [_rec("s03", True)], label="infra-retry")
    p = evals.result_path(r, tmp_path)
    assert p.parent.name == "reruns" and p.name == "claude-cli-sonnet-infra-retry.json"


def test_merge_replaces_only_rerun_ids_and_keeps_recorded_summary():
    base = _run("sonnet", "claude-sonnet-5-5", "questions",
                [_rec("s01", True), _rec("s03", False, "error", "timed out"), _rec("j03", False)])
    rerun = _run("sonnet", "claude-sonnet-5-5", "questions", [_rec("s03", True)], label="infra-retry")
    out = evals.merge_results(base, [rerun], "base.json", ["rerun.json"])
    assert {r["id"]: r["correct"] for r in out["records"]} == {"s01": True, "s03": True, "j03": False}
    assert out["derived_from"]["base_summary"]["overall_accuracy_pct"] == pytest.approx(33.3)
    assert out["summary"]["overall_accuracy_pct"] == pytest.approx(66.7)
    assert out["derived_from"]["reruns"][0]["replaced_case_ids"] == ["s03"]


def test_merge_rejects_mismatched_tier():
    base = _run("sonnet", "claude-sonnet-5-5", "questions", [_rec("s01", True)])
    other = _run("haiku", "claude-haiku-5-5", "questions", [_rec("s01", True)])
    with pytest.raises(ValueError):
        evals.merge_results(base, [other])


# ---- j03 corrected gold accepts the labelled answer ----

def test_j03_labelled_unknown_answer_is_correct(con):
    gold = next(c for c in evals.load_cases() if c["id"] == "j03")["gold_sql"]
    model = ("SELECT COALESCE(cu.region, 'Unknown') AS region, COUNT(*) AS claim_count FROM claims c "
             "JOIN policies p ON p.policy_id = c.policy_id JOIN customers cu ON cu.customer_id = p.customer_id "
             "GROUP BY COALESCE(cu.region, 'Unknown') ORDER BY region")
    assert result_sets_match(con.execute(gold).fetchall(), con.execute(model).fetchall())
    assert "'Unknown'" in next(c for c in evals.load_cases() if c["id"] == "j03")["question"]
