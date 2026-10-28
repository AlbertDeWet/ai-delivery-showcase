import datetime as dt

from nl2sql.grader import result_sets_match


def test_identical():
    assert result_sets_match([(1, "a")], [(1, "a")])


def test_order_insensitive():
    assert result_sets_match([("a", 1), ("b", 2)], [("b", 2), ("a", 1)])


def test_numeric_tolerance_rounding():
    # Grader v2: rounding is absorbed at the gold's precision (money to the cent, ratios to 4 dp).
    assert result_sets_match([(0.61234567,)], [(0.6123,)])
    assert result_sets_match([(1234567.89,)], [(1234567.8912,)])
    # Changed in round 1 (R2): a 3-dp gold no longer accepts a 2-dp prediction that differs at 3 dp.
    assert not result_sets_match([(1234567.891,)], [(1234567.89,)])


def test_money_tolerance_is_absolute_half_cent():
    assert result_sets_match([(7368797.02,)], [(7368797.024,)])
    assert not result_sets_match([(7368797.02,)], [(7368797.03,)])
    assert not result_sets_match([(7368797.02,)], [(7368714.14,)])  # one 82.88 row dropped


def test_ratio_tolerance_is_four_decimals_even_above_one():
    assert result_sets_match([(1.2345,)], [(1.23449,)])
    assert not result_sets_match([(1.2345,)], [(1.2399,)])


def test_numeric_difference_detected():
    assert not result_sets_match([(100.0,)], [(101.0,)])


def test_integral_values_compared_exactly():
    # 1000 vs 1001 is within 1e-3 relative tolerance, but counts must match exactly.
    assert not result_sets_match([(1000,)], [(1001,)])
    assert not result_sets_match([(1000.0,)], [(1001.0,)])
    assert not result_sets_match([("North", 5000)], [("North", 5001)])


def test_integral_gold_requires_integral_prediction():
    # Changed in round 1 (R1): integral gold is exact; a fractional prediction fails.
    assert not result_sets_match([(5000.0,)], [(5000.001,)])
    assert not result_sets_match([(1000,)], [(1000.4,)])
    assert result_sets_match([(1000,)], [(1000.0,)])


def test_single_row_wide_prediction_must_align():
    assert result_sets_match([(1206,)], [("label", 1206)])  # right-aligned
    assert result_sets_match([(1206,)], [(1206, 99)])  # left-aligned
    assert not result_sets_match([(1206,)], [(1, 1206, 2)])  # middle: no "any column" search
    assert result_sets_match([(1206,)], [(1, 1206, 2)], gold_cols=["n"], pred_cols=["a", "n", "b"])


def test_nan_matches_only_nan():
    assert not result_sets_match([(None,)], [(float("nan"),)])
    assert result_sets_match([(float("nan"),)], [(float("nan"),)])


def test_int_vs_float():
    assert result_sets_match([(42,)], [(42.0,)])


def test_row_count_mismatch():
    assert not result_sets_match([(1,), (2,)], [(1,)])


def test_duplicate_rows_matter():
    assert not result_sets_match([(1,), (1,)], [(1,), (2,)])


def test_extra_predicted_column_is_projected():
    gold = [("Motor", 10), ("Travel", 5)]
    pred = [(1, "Motor", 10), (2, "Travel", 5)]
    assert result_sets_match(gold, pred)


def test_column_order_ignored():
    assert result_sets_match([("Motor", 10)], [(10, "Motor")])


def test_missing_column_fails():
    assert not result_sets_match([("Motor", 10)], [(10,)])


def test_none_handling():
    assert result_sets_match([(None, 3)], [(None, 3)])
    assert not result_sets_match([(None, 3)], [("North", 3)])


def test_dates_compare_by_value():
    assert result_sets_match([(dt.date(2024, 7, 1),)], [(dt.date(2024, 7, 1),)])


def test_numeric_string_vs_number():
    assert result_sets_match([(2024, 5.0)], [("2024", 5.0)])


def test_empty_sets():
    assert result_sets_match([], [])


def test_different_sql_same_result_is_correct(con):
    # Grading is by execution: textually different SQL with equal results passes.
    a = con.execute("SELECT COUNT(*) FROM policies WHERE LOWER(status) = 'active'").fetchall()
    b = con.execute("SELECT SUM(CASE WHEN UPPER(status) = 'ACTIVE' THEN 1 ELSE 0 END) FROM policies").fetchall()
    assert result_sets_match(a, b)


def test_case_sensitive_trap_is_caught(con):
    gold = con.execute("SELECT COUNT(*) FROM policies WHERE LOWER(status) = 'active'").fetchall()
    naive = con.execute("SELECT COUNT(*) FROM policies WHERE status = 'Active'").fetchall()
    assert not result_sets_match(gold, naive)
