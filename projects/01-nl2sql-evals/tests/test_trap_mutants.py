"""Mutation tests for the eval traps (review items 9 and 10).

Each entry is a plausible wrong query (an off-by-one date filter, a case-sensitive
status filter, an inner join that drops NULLs, ...). Every mutant must grade as
WRONG against the gold answer; if one ever passes, that trap has stopped biting.
"""

import pytest

from nl2sql import evals, synth
from nl2sql.grader import result_sets_match

GOLD = {c["id"]: c["gold_sql"] for c in evals.load_cases() if c.get("gold_sql")}

FY_PREM = "SELECT ROUND(SUM(amount), 2) FROM premiums WHERE {}"
FY_POL = "SELECT COUNT(*) FROM policies WHERE {}"
FY_CLM = "SELECT SUM(COALESCE(paid_amount, 0) + reserve_amount) FROM claims WHERE {}"
LR = ("SELECT ROUND(({inc}) / (SELECT SUM(amount) FROM premiums WHERE "
      "transaction_date BETWEEN DATE '2024-07-01' AND DATE '2025-06-30'), 4)")

MUTANTS = {
    # t02: GWP FY2025 = 2024-07-01 .. 2025-06-30
    "t02": [
        FY_PREM.format("transaction_date > DATE '2024-07-01' AND transaction_date <= DATE '2025-06-30'"),
        FY_PREM.format("transaction_date >= DATE '2024-07-01' AND transaction_date < DATE '2025-06-30'"),
        FY_PREM.format("transaction_date BETWEEN DATE '2024-06-30' AND DATE '2025-06-29'"),
        FY_PREM.format("transaction_date BETWEEN DATE '2024-07-01' AND DATE '2025-07-01'"),
        FY_PREM.format("transaction_date BETWEEN DATE '2025-07-01' AND DATE '2026-06-30'"),  # named by start year
        FY_PREM.format("EXTRACT(year FROM transaction_date) = 2025"),  # calendar year
    ],
    # t06: policies started in FY2024 = 2023-07-01 .. 2024-06-30
    "t06": [
        FY_POL.format("start_date > DATE '2023-07-01' AND start_date <= DATE '2024-06-30'"),
        FY_POL.format("start_date >= DATE '2023-07-01' AND start_date < DATE '2024-06-30'"),
        FY_POL.format("start_date BETWEEN DATE '2023-06-30' AND DATE '2024-06-29'"),
        FY_POL.format("start_date BETWEEN DATE '2023-07-02' AND DATE '2024-07-01'"),
    ],
    # m07: loss ratio FY2025, claims side off by one day
    "m07": [
        LR.format(inc=FY_CLM.format("loss_date > DATE '2024-07-01' AND loss_date <= DATE '2025-06-30'")),
        LR.format(inc=FY_CLM.format("loss_date >= DATE '2024-07-01' AND loss_date < DATE '2025-06-30'")),
    ],
    # t03: GWP by fiscal year with the boundary month wrong
    "t03": [
        "SELECT EXTRACT(year FROM transaction_date) + CASE WHEN EXTRACT(month FROM transaction_date) > 7 "
        "THEN 1 ELSE 0 END AS fy, ROUND(SUM(amount), 2) FROM premiums GROUP BY fy",
        "SELECT EXTRACT(year FROM transaction_date) AS fy, ROUND(SUM(amount), 2) FROM premiums GROUP BY fy",
    ],
    # m06: reversal trap
    "m06": [
        "SELECT ROUND(SUM(pr.amount), 2) FROM premiums pr JOIN policies p ON p.policy_id = pr.policy_id "
        "JOIN products pd ON pd.product_id = p.product_id "
        "WHERE pd.line_of_business = 'Motor' AND pr.transaction_type = 'WRITTEN'",
        "SELECT ROUND(SUM(ABS(pr.amount)), 2) FROM premiums pr JOIN policies p ON p.policy_id = pr.policy_id "
        "JOIN products pd ON pd.product_id = p.product_id WHERE pd.line_of_business = 'Motor'",
    ],
    # x01 / x02 / x03: case-sensitive status filters
    "x01": ["SELECT COUNT(*) FROM policies WHERE status = 'Active'"],
    "x02": ["SELECT COUNT(*) FROM claims WHERE status = 'Open'"],
    "x03": ["SELECT COUNT(*) FROM policies WHERE status = 'Cancelled'"],
    # s08: distinct status without normalising case
    "s08": ["SELECT DISTINCT status FROM claims"],
    # j04: inner join drops direct business
    "j04": ["SELECT b.broker_name, ROUND(SUM(COALESCE(c.paid_amount, 0)), 2) FROM claims c "
            "JOIN policies p ON p.policy_id = c.policy_id JOIN brokers b ON b.broker_id = p.broker_id "
            "GROUP BY b.broker_name"],
    # j03: NULL region dropped; NULL group left unlabelled (the pre-fix gold)
    "j03": ["SELECT cu.region, COUNT(*) FROM claims c JOIN policies p ON p.policy_id = c.policy_id "
            "JOIN customers cu ON cu.customer_id = p.customer_id WHERE cu.region IS NOT NULL GROUP BY cu.region",
            "SELECT cu.region, COUNT(*) FROM claims c JOIN policies p ON p.policy_id = c.policy_id "
            "JOIN customers cu ON cu.customer_id = p.customer_id GROUP BY cu.region"],
    # x05: AVG over NULL paid amounts ignores unpaid claims
    "x05": ["SELECT ROUND(AVG(paid_amount), 2) FROM claims"],
    # m09: closed claims, case-sensitive
    "m09": ["SELECT ROUND(SUM(COALESCE(paid_amount, 0)), 2) FROM claims "
            "WHERE status = 'Closed' AND EXTRACT(year FROM loss_date) = 2024"],
}

CASES = [(cid, i) for cid, ms in sorted(MUTANTS.items()) for i in range(len(ms))]


@pytest.mark.parametrize("cid,i", CASES, ids=[f"{c}-mutant{i}" for c, i in CASES])
def test_mutant_is_graded_wrong(cid, i, con):
    gold = con.execute(GOLD[cid]).fetchall()
    mutant = con.execute(MUTANTS[cid][i]).fetchall()
    assert not result_sets_match(gold, mutant), f"trap {cid} no longer catches mutant {i}"


def test_boundary_rows_planted_on_both_sides(con):
    for d, _ in synth.BOUNDARY_ROWS:
        iso = d.isoformat()
        for table, col in (("premiums", "transaction_date"), ("policies", "start_date"), ("claims", "loss_date")):
            n = con.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = DATE '{iso}'").fetchone()[0]
            assert n >= 1, (table, iso)
