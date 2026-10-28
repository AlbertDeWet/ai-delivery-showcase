"""ADVERSARY: Round 2 attacks on the Round 1 fixes (grader v2, attempts, not-run rule, CTE scoping)."""

import json

import pytest

from nl2sql import cli, evals, guard
from nl2sql.grader import result_sets_match

from ._helpers import rec, run


# ---- grader v2: positional / by-name matching ----

def test_one_row_two_candidate_hedge_fails():
    """ADVERSARY: one-row answers accept the gold at the first OR last column, so a 2-number hedge
    (1205, 1206) still passes against gold 1206."""
    assert not result_sets_match([(1206,)], [(1205, 1206)])


def test_multi_row_hedge_columns_fail():
    """ADVERSARY: multi-row answers still search any column subset, so a per-group answer with three
    candidate measures (9, 10, 11 / 19, 20, 21) passes if one column happens to be right."""
    gold = [("A", 10), ("B", 20)]
    pred = [("A", 9, 10, 11), ("B", 19, 20, 21)]
    assert not result_sets_match(gold, pred)


def test_wrong_column_in_the_middle_is_not_accepted():
    """ADVERSARY: positive control - a 2-column gold cannot be satisfied by skipping a wrong middle column."""
    assert not result_sets_match([("North", 68)], [("North", 999, 68)])


def test_wrong_end_column_with_right_start_is_judged_on_the_start():
    """ADVERSARY: positive control - the gold aligned left matches; a wrong trailing extra column is ignored."""
    assert result_sets_match([("North", 68)], [("North", 68, 999)])


# ---- precision-based tolerance ----

def test_integral_gold_tolerates_float_summation_noise():
    """ADVERSARY: integral gold demands an exactly integral prediction, so a DOUBLE sum that lands on
    1000.0000000001 (different summation order) is graded wrong. False negative."""
    assert result_sets_match([(1000.0,)], [(1000.0000000001,)])


def test_trailing_zero_gold_ratio_is_strict_at_4dp():
    """ADVERSARY: positive control - gold 0.10 (stored 0.1) is read as a ratio at 4 dp; 0.1004 fails."""
    assert not result_sets_match([(0.1,)], [(0.1004,)])


def test_trailing_zero_money_gold_is_strict_at_cents():
    """ADVERSARY: positive control - gold 12.50 (stored 12.5) is money at 2 dp; 12.51 fails, 12.504 passes."""
    assert not result_sets_match([(12.5,)], [(12.51,)])
    assert result_sets_match([(12.5,)], [(12.504,)])


def test_integral_percentage_gold_rejects_one_decimal_drift():
    """ADVERSARY: positive control - gold 45.0 is integral, so 45.004 fails (no relative tolerance)."""
    assert not result_sets_match([(45.0,)], [(45.004,)])


# ---- attempt numbering ----

def _files(tmp_path):
    base = run([rec(c, c != "s03", repeat=k) for c in ("s01", "s03") for k in range(3)], repeats=3)
    bp = tmp_path / "claude-cli-haiku.json"
    bp.write_text(json.dumps(base))
    bad = run([rec("s03", False, repeat=k) for k in range(3)], repeats=3, label="retry")
    good = run([rec("s03", True, repeat=k) for k in range(3)], repeats=3, label="retry")
    p1 = evals.save(bad, tmp_path)
    p2 = evals.save(good, tmp_path)
    return bp, p1, p2


def _merge(bp, rerun, out):
    return cli.main(["merge", "--base", str(bp), "--rerun", str(rerun), "--out", str(out),
                     "--results-dir", str(bp.parent)])


def test_attempts_are_numbered_and_never_overwritten(tmp_path):
    """ADVERSARY: positive control - two saves of one label give attempt1 and attempt2."""
    _, p1, p2 = _files(tmp_path)
    assert p1 != p2 and p1.name.endswith("attempt1.json") and p2.name.endswith("attempt2.json")


def test_merge_refuses_when_an_attempt_is_left_out(tmp_path):
    """ADVERSARY: positive control - passing only the lucky attempt2 while attempt1 is on disk is refused."""
    bp, _, p2 = _files(tmp_path)
    assert _merge(bp, p2, tmp_path / "derived" / "d.json") != 0


def test_deleted_attempt_leaves_a_detectable_gap(tmp_path):
    """ADVERSARY: delete attempt1 (the failure) and merge attempt2 alone. Attempt numbers start at 2,
    which proves an attempt is missing, yet merge accepts it."""
    bp, p1, p2 = _files(tmp_path)
    p1.unlink()
    assert _merge(bp, p2, tmp_path / "derived" / "d.json") != 0


def test_copied_attempt_elsewhere_cannot_hide_siblings(tmp_path):
    """ADVERSARY: attempts are counted only in the folder of the supplied file. Copy the lucky
    attempt2 to another folder and pass that copy: the failed attempt1 is never counted."""
    bp, _, p2 = _files(tmp_path)
    elsewhere = tmp_path / "scratch"
    elsewhere.mkdir()
    copy = elsewhere / p2.name
    copy.write_text(p2.read_text())
    assert _merge(bp, copy, tmp_path / "derived" / "d.json") != 0


def test_relabelled_attempt_cannot_escape_its_series(tmp_path):
    """ADVERSARY: edit attempt2's label (and rename the file) so it forms its own one-attempt series."""
    bp, _, p2 = _files(tmp_path)
    d = json.loads(p2.read_text())
    d["label"] = "fresh"
    d["attempt"] = 1
    forged = p2.parent / "claude-cli-haiku-fresh-attempt1.json"
    forged.write_text(json.dumps(d))
    p2.unlink()
    assert _merge(bp, forged, tmp_path / "derived" / "d.json") != 0


# ---- "not run" rule ----

def _rows(tmp_path, records):
    (tmp_path / "claude-cli-haiku.json").write_text(json.dumps(run(records)))
    return [l for l in evals.render_report(tmp_path).splitlines() if "claude-haiku-5-5 | questions" in l]


def test_execution_error_with_zero_tokens_is_still_a_result(tmp_path):
    """ADVERSARY: an execution error whose token count is missing (0) must not be read as 'not run'."""
    rows = _rows(tmp_path, [rec("s01", False, "error", "execution error: Binder Error")])
    assert rows and not any("not run" in r for r in rows)


def test_timeouts_alone_are_not_run(tmp_path):
    """ADVERSARY: positive control - zero-token CLI timeouts only render as 'not run'."""
    rows = _rows(tmp_path, [rec("s01", False, "error", "claude CLI failed: timed out after 180 seconds")])
    assert rows and all("not run" in r for r in rows)


# ---- CTE scoping ----

@pytest.mark.parametrize("sql", [
    "SELECT * FROM 'f.csv', (WITH \"f.csv\" AS (SELECT 1) SELECT * FROM \"f.csv\") t",
    "(WITH x AS (SELECT 1) SELECT * FROM x) UNION ALL SELECT * FROM x",
    "SELECT * FROM (WITH x AS (SELECT 1) SELECT * FROM x) a, x",
    "SELECT (WITH x AS (SELECT 1) SELECT * FROM x), * FROM x",
    "WITH \"data.parquet\" AS (SELECT 1) SELECT * FROM 'data.parquet'",
])
def test_cte_names_do_not_leak_out_of_scope(sql):
    """ADVERSARY: a CTE must not whitelist a name outside its own query, and a CTE may not be named
    like a file path."""
    assert not guard.check(sql).ok


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT 1 AS a) SELECT * FROM policies WHERE policy_id IN (SELECT a FROM x)",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM r WHERE n<3) SELECT * FROM r",
    "WITH x AS MATERIALIZED (SELECT 1) SELECT * FROM x",
])
def test_legitimate_cte_use_still_passes(sql):
    """ADVERSARY: positive control - nested, recursive and materialized CTE use is not falsely blocked."""
    assert guard.check(sql).ok
