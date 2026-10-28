"""ADVERSARY: attacks on execution grading (nl2sql/grader.py)."""

from nl2sql.grader import result_sets_match


def test_integer_gold_is_exact_even_when_prediction_is_fractional():
    """ADVERSARY: CHANGES #1 says counts compare exactly, but the exact rule needs BOTH sides integral.
    A count of 1000 against a predicted 1000.4 passes on the 1e-3 tolerance."""
    assert not result_sets_match([(1000,)], [(1000.4,)])


def test_dropping_a_premium_row_from_gwp_fails(con):
    """ADVERSARY: rel tol 1e-3 on a ~7.4M GWP total is ~7.4k; a query that silently drops a real
    premium row still grades as correct."""
    gold = con.execute("SELECT ROUND(SUM(amount), 2) FROM premiums").fetchall()
    pred = con.execute("SELECT ROUND(SUM(amount), 2) FROM premiums WHERE premium_id <> "
                       "(SELECT premium_id FROM premiums ORDER BY ABS(amount) LIMIT 1)").fetchall()
    assert gold != pred
    assert not result_sets_match(gold, pred)


def test_shotgun_wide_prediction_does_not_pass():
    """ADVERSARY: a single-row prediction listing several candidate numbers passes if any one column
    matches, because extra predicted columns are projected away (up to 8 columns)."""
    assert not result_sets_match([(1206,)], [(1200, 1201, 1202, 1203, 1204, 1205, 1206, 1207)])


def test_nan_is_not_null():
    """ADVERSARY: NaN is normalised to None, so a NaN answer matches a NULL gold."""
    assert not result_sets_match([(None,)], [(float("nan"),)])


def test_off_by_one_count_still_fails():
    """ADVERSARY: positive control for CHANGES #1 when both sides are integral."""
    assert not result_sets_match([(1000,)], [(1001,)])
