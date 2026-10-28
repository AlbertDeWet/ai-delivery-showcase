"""ADVERSARY: the all-error guard in render_report (nl2sql/evals.py)."""

import json

from nl2sql import evals

from ._helpers import rec, run


def _rows(tmp_path, records):
    (tmp_path / "claude-cli-haiku.json").write_text(json.dumps(run(records)))
    return [l for l in evals.render_report(tmp_path).splitlines() if "claude-haiku-5-5 | questions" in l]


def test_all_infra_errors_render_as_not_run(tmp_path):
    """ADVERSARY: positive control - a run of zero-token infrastructure errors is 'not run'."""
    rows = _rows(tmp_path, [rec("s01", False, "error", "claude CLI failed: timed out")])
    assert rows and all("not run" in r for r in rows)


def test_model_execution_errors_are_not_hidden_as_not_run(tmp_path):
    """ADVERSARY: a model whose SQL always fails to execute is a 0% model result, but every record
    has action 'error', so the report renders it as 'not run: execution error ...'."""
    recs = [rec(c, False, "error", "execution error: Binder Error", in_tok=1800, out_tok=30)
            for c in ("s01", "s02")]
    rows = _rows(tmp_path, recs)
    assert rows and not any("not run" in r for r in rows)


def test_unparseable_model_output_is_not_hidden_as_not_run(tmp_path):
    """ADVERSARY: token-consuming unparseable answers are model failures, yet render as 'not run'."""
    recs = [rec(c, False, "error", "unparseable model output: no JSON", in_tok=1800, out_tok=30)
            for c in ("s01", "s02")]
    rows = _rows(tmp_path, recs)
    assert rows and not any("not run" in r for r in rows)


def test_nearly_all_infra_error_run_is_flagged(tmp_path):
    """ADVERSARY: one success among many infra errors defeats the all-error guard; the run renders as
    a plain low score with no infrastructure warning."""
    recs = [rec(f"c{i}", False, "error", "claude CLI failed: timed out") for i in range(20)] + [rec("ok", True)]
    rows = _rows(tmp_path, recs)
    assert rows and any("not run" in r or "infra" in r.lower() for r in rows)
