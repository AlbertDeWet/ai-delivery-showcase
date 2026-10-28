"""Builder tests for the adversarial round 2 fixes (A6-A8, R5-R7)."""

import json
from pathlib import Path

import pytest

from nl2sql import evals
from nl2sql.grader import result_sets_match

ROOT = Path(__file__).resolve().parent.parent


# ---- R5/R6: hedges fail, real context-column answers still pass ----

def test_j01_shape_with_context_columns_passes():
    # Recorded Haiku answer shape: product_id, product_name, line_of_business, policy_count.
    gold = [("Contents", 160), ("Homeowners", 159), ("Motor Comprehensive", 144)]
    pred = [(4, "Contents", "Property", 160), (3, "Homeowners", "Property", 159),
            (1, "Motor Comprehensive", "Motor", 144)]
    assert result_sets_match(gold, pred)


def test_m02_shape_with_gwp_and_incurred_context_passes():
    gold = [("Commercial", 0.665), ("Motor", 0.1645)]
    pred = [("Commercial", 910550.26, 605479.91, 0.665), ("Motor", 3624152.06, 596192.42, 0.1645)]
    assert result_sets_match(gold, pred)


def test_multi_row_near_candidate_hedge_fails():
    gold = [("A", 100), ("B", 200)]
    assert not result_sets_match(gold, [("A", 100, 104), ("B", 200, 195)])


def test_one_row_label_plus_value_still_passes():
    assert result_sets_match([(1206,)], [("total policies", 1206)])


def test_integral_noise_bound():
    assert result_sets_match([(1000,)], [(1000.0000001,)])
    assert not result_sets_match([(1000,)], [(1000.00001,)])


# ---- A6-A8: completeness over every stored attempt ----

def _run(cid, ok, label, attempt=None):
    recs = [{"id": cid, "category": "simple", "repeat": k, "expect": ["answer"], "question": "q",
             "action": "sql", "correct": ok, "reason": "", "sql": "", "guard_reasons": [], "error": "",
             "latency_s": 1.0, "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "retries": 0,
             "first_error": "", "check": {"status": "abstain", "key_number": None, "control": None,
                                          "note": "", "input_tokens": 0, "output_tokens": 0}} for k in range(3)]
    r = {"provider": "claude-cli", "tier": "haiku", "model": "claude-haiku-5-5", "suite": "questions",
         "repeats": 3, "label": label, "timestamp": "t", "records": recs, "summary": evals.summarize(recs, 3)}
    if attempt:
        r["attempt"] = attempt
    return r


BASE = {"provider": "claude-cli", "tier": "haiku", "model": "claude-haiku-5-5", "suite": "questions"}


def test_gap_in_attempts_is_refused(tmp_path):
    (tmp_path / "a2.json").write_text(json.dumps(_run("s03", True, "x", 2)))
    with pytest.raises(ValueError, match="gap"):
        evals.check_rerun_completeness(BASE, [_run("s03", True, "x", 2)], [(tmp_path, True)])


def test_other_label_covering_same_case_must_be_reported(tmp_path):
    (tmp_path / "x1.json").write_text(json.dumps(_run("s03", False, "x", 1)))
    (tmp_path / "y1.json").write_text(json.dumps(_run("s03", True, "y", 1)))
    with pytest.raises(ValueError, match="not supplied"):
        evals.check_rerun_completeness(BASE, [_run("s03", True, "y", 1)], [(tmp_path, True)])


def test_unrelated_series_does_not_block(tmp_path):
    (tmp_path / "x1.json").write_text(json.dumps(_run("m10", False, "x", 1)))
    out = evals.check_rerun_completeness(BASE, [_run("s03", True, "y", 1)], [(tmp_path, True)])
    assert out == {"claude-cli-haiku-x": 1, "claude-cli-haiku-y": 1}


def test_real_recorded_merges_pass_the_completeness_check():
    rr = ROOT / "results" / "reruns"
    base = json.loads((ROOT / "results" / "claude-cli-sonnet.json").read_text(encoding="utf-8"))
    supplied = [json.loads((rr / n).read_text(encoding="utf-8"))
                for n in ("claude-cli-sonnet-j03-fix.json", "claude-cli-sonnet-infra-retry.json")]
    on_disk = evals.check_rerun_completeness(base, supplied, [(rr, True)])
    assert on_disk == {"claude-cli-sonnet-j03-fix": 1, "claude-cli-sonnet-infra-retry": 1}
