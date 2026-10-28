"""The eval set itself is tested: every gold answer must run, pass the guard and grade as correct."""

import pytest

from nl2sql import checker, evals, guard, pipeline
from nl2sql.grader import result_sets_match
from nl2sql.providers.baseline import BaselineProvider

CASES = evals.load_cases()
ANSWERABLE = [c for c in CASES if "answer" in c["expect"]]


def test_at_least_40_cases_with_unique_ids():
    assert len(CASES) >= 40
    assert len({c["id"] for c in CASES}) == len(CASES)


def test_required_adversarial_categories_present():
    cats = {c["category"] for c in CASES}
    for need in ("adversarial_destructive", "adversarial_injection", "out_of_schema", "ambiguous"):
        assert need in cats


@pytest.mark.parametrize("case", ANSWERABLE, ids=[c["id"] for c in ANSWERABLE])
def test_gold_sql_passes_guard_and_returns_rows(case, con):
    g = guard.check(case["gold_sql"])
    assert g.ok, g.reasons
    rows = con.execute(g.sql).fetchall()
    assert rows, "gold query returned no rows"
    assert result_sets_match(rows, con.execute(case["gold_sql"]).fetchall())


def test_reversal_trap_changes_the_answer(con):
    gold = con.execute(next(c for c in CASES if c["id"] == "m06")["gold_sql"]).fetchall()
    naive = con.execute("""SELECT ROUND(SUM(pr.amount), 2) FROM premiums pr
        JOIN policies p ON p.policy_id = pr.policy_id JOIN products pd ON pd.product_id = p.product_id
        WHERE pd.line_of_business = 'Motor' AND pr.transaction_type = 'WRITTEN'""").fetchall()
    assert not result_sets_match(gold, naive)


def test_fiscal_year_trap_changes_the_answer(con):
    gold = con.execute(next(c for c in CASES if c["id"] == "t02")["gold_sql"]).fetchall()
    calendar = con.execute("SELECT ROUND(SUM(amount), 2) FROM premiums "
                           "WHERE EXTRACT(year FROM transaction_date) = 2025").fetchall()
    assert not result_sets_match(gold, calendar)


def test_baseline_refuses_destructive_cases(con):
    for c in CASES:
        if c["category"] == "adversarial_destructive":
            assert pipeline.ask(c["question"], BaselineProvider(), con).action in ("refuse", "blocked")


def test_checker_flags_wrong_number(con):
    res = pipeline.AskResult(question="How many claims are there?", action="sql",
                             columns=["n"], rows=[(1,)])
    assert checker.check(res, BaselineProvider(), con).status == "disagree"


def test_checker_agrees_on_right_number(con):
    n = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    res = pipeline.AskResult(question="How many claims are there?", action="sql", columns=["n"], rows=[(n,)])
    assert checker.check(res, BaselineProvider(), con).status == "agree"


def test_checker_abstains_on_ratio(con):
    res = pipeline.AskResult(question="What is our overall loss ratio?", action="sql",
                             columns=["lr"], rows=[(0.5,)])
    assert checker.check(res, BaselineProvider(), con).status == "abstain"


def test_key_number_sums_last_numeric_column():
    assert checker.key_number(["a", "y", "v"], [("x", 2024, 1.5), ("y", 2025, 2.5)]) == 4.0


HELDOUT = evals.load_cases(evals.config.REPO_ROOT / "evals" / "heldout.yaml")
HELDOUT_ANSWERABLE = [c for c in HELDOUT if "answer" in c["expect"]]


def test_heldout_set_has_12_plus_cases_and_no_id_overlap():
    assert len(HELDOUT) >= 12
    assert not {c["id"] for c in HELDOUT} & {c["id"] for c in CASES}
    assert not {c["question"] for c in HELDOUT} & {c["question"] for c in CASES}


@pytest.mark.parametrize("case", HELDOUT_ANSWERABLE, ids=[c["id"] for c in HELDOUT_ANSWERABLE])
def test_heldout_gold_sql_passes_guard_and_returns_rows(case, con):
    g = guard.check(case["gold_sql"])
    assert g.ok, g.reasons
    assert con.execute(g.sql).fetchall()
