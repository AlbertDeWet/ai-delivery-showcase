"""ADVERSARY: can derived results (python -m nl2sql merge) be cherry-picked or forged?"""

import pytest

from nl2sql import evals

from ._helpers import rec, run


def _base():
    recs = [rec(c, c != "s03", repeat=k) for c in ("s01", "s02", "s03") for k in range(3)]
    return run(recs, repeats=3)


def _three(cid, ok):
    return [rec(cid, ok, repeat=k) for k in range(3)]


def test_rerun_cannot_add_case_ids_absent_from_base():
    """ADVERSARY: a re-run with an extra, easy case id is appended, inflating n and accuracy."""
    with pytest.raises(ValueError):
        evals.merge_results(_base(), [run(_three("zz9", True), repeats=3)])


def test_rerun_must_have_repeats_records_per_case():
    """ADVERSARY: a re-run claiming repeats=3 but holding one passing record per case is accepted,
    so pass^k for that case is judged on 1 attempt instead of 3."""
    with pytest.raises(ValueError):
        evals.merge_results(_base(), [run([rec("s03", True)], repeats=3)])


def test_rerun_with_a_different_model_is_rejected():
    """ADVERSARY: provider/tier/suite are checked, the model id is not."""
    with pytest.raises(ValueError):
        evals.merge_results(_base(), [run(_three("s03", True), repeats=3, model="claude-sonnet-5-5")])


def test_saving_a_rerun_never_overwrites_earlier_rerun_evidence(tmp_path):
    """ADVERSARY: re-running with the same --label overwrites results/reruns/<file>.json, so failed
    re-run attempts can be erased and only the lucky one kept (cherry-picking by repetition)."""
    p1 = evals.save(run(_three("s03", False), repeats=3, label="retry"), tmp_path)
    p2 = evals.save(run(_three("s03", True), repeats=3, label="retry"), tmp_path)
    assert p1 != p2


def test_derived_result_states_the_retry_rule_it_mixes():
    """ADVERSARY: base runs recorded before the rule have no retry_rule key; the derived file copies
    base keys, so it carries no top-level retry_rule although its re-run records used one."""
    rr = run(_three("s03", True), repeats=3)
    rr["retry_rule"] = evals.RETRY_RULE
    assert "retry_rule" in evals.merge_results(_base(), [rr])


def test_merge_recomputes_and_keeps_recorded_summary():
    """ADVERSARY: positive control - recorded summary preserved, derived summary recomputed."""
    base = _base()
    out = evals.merge_results(base, [run(_three("s03", True), repeats=3)])
    assert out["derived_from"]["base_summary"] == base["summary"]
    assert out["summary"] == evals.summarize(out["records"], 3)
