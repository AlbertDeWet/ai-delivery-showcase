"""ADVERSARY round 1: broken-measure detection, regression grader, consumer check."""
from pathlib import Path

import duckdb
from helpers import FACT, make_model, make_reports

from smguard.analyze import broken_measures, consumer_check, report_fields
from smguard.regress import classify, run_suite
from smguard.tmdl import load_model


def _model_with(tmp_path, *measures):
    fact = FACT + "".join(f"\n\tmeasure {m}\n" for m in measures)
    return load_model(make_model(tmp_path, fact=fact))


# ---------- broken-measure detection ----------

def test_comment_reference_is_not_a_break(tmp_path):
    """ADVERSARY: a [Name] inside a DAX // comment does not make a valid measure 'broken' (false positive)."""
    m = _model_with(tmp_path, "X = SUM(FactClaims[PaidAmount]) // was [Old Paid]")
    assert "X" not in broken_measures(m)


def test_case_insensitive_table_name_is_not_a_break(tmp_path):
    """ADVERSARY: DAX names are case-insensitive; factclaims[paidamount] is valid, not broken."""
    m = _model_with(tmp_path, "X = SUM(factclaims[paidamount])")
    assert "X" not in broken_measures(m)


def test_table_qualified_measure_ref_is_not_a_break(tmp_path):
    """ADVERSARY: FactClaims[Total Paid] (table-qualified measure reference, legal DAX) is not 'missing column'."""
    m = _model_with(tmp_path, "X = FactClaims[Total Paid] * 2")
    assert "X" not in broken_measures(m)


def test_missing_table_in_countrows_is_detected(tmp_path):
    """ADVERSARY: COUNTROWS(GhostTable) references a table that does not exist -> broken (false negative)."""
    m = _model_with(tmp_path, "X = COUNTROWS(GhostTable)")
    assert "X" in broken_measures(m)


def test_self_reference_is_detected(tmp_path):
    """ADVERSARY: a measure that references itself is circular -> broken."""
    m = _model_with(tmp_path, "X = [X] + 1")
    assert "X" in broken_measures(m)


def test_bare_column_ref_is_detected(tmp_path):
    """ADVERSARY: [GhostCol] with no table is checked against measures; SUM([GhostCol]) is broken."""
    m = _model_with(tmp_path, "X = SUM([GhostCol])")
    assert "X" in broken_measures(m)


# ---------- regression grader ----------

def _data(d: Path, fact_rows):
    d.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE f(PolicyKey BIGINT, DateKey BIGINT, PaidAmount DOUBLE, ReserveAmount DOUBLE, ClaimStatus VARCHAR)")
    con.executemany("INSERT INTO f VALUES (?,?,?,?,?)", fact_rows)
    con.execute(f"COPY f TO '{(d / 'FactClaims.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT 1::BIGINT PolicyKey, 'Motor' LineOfBusiness, 'North' Region) TO '{(d / 'DimPolicy.parquet').as_posix()}' (FORMAT parquet)")
    con.execute(f"COPY (SELECT 1::BIGINT DateKey, 2025::BIGINT \"Year\", 1::BIGINT \"Month\", 'Jan' MonthName) TO '{(d / 'DimDate.parquet').as_posix()}' (FORMAT parquet)")
    con.close()
    return d


def test_model_change_masked_as_fresh_data(tmp_path):
    """ADVERSARY: a real definition change that only shows on new data must not be graded 'EXPECTED: fresh data'."""
    before_m = make_model(tmp_path / "b", fact=FACT + "\n\tmeasure Reopened = CALCULATE(COUNTROWS(FactClaims), FactClaims[ClaimStatus] = \"Reopened\")\n")
    after_m = make_model(tmp_path / "a", fact=FACT + "\n\tmeasure Reopened = CALCULATE(COUNTROWS(FactClaims), FactClaims[ClaimStatus] = \"Closed\")\n")
    old = _data(tmp_path / "old", [(1, 1, 10.0, 5.0, "Open")])
    new = _data(tmp_path / "new", [(1, 1, 10.0, 5.0, "Open"), (1, 1, 3.0, 1.0, "Reopened"), (1, 1, 4.0, 0.0, "Closed")])
    rows = classify(run_suite(before_m, old), run_suite(after_m, old), run_suite(after_m, new))
    bad = [r for r in rows if r["check"] == "total::Reopened"][0]
    assert bad["class"] == "UNEXPECTED", bad


def test_untranslatable_measure_removed_is_not_silently_expected(tmp_path):
    """ADVERSARY: a VALID but untranslatable measure (SUMX) that disappears is graded 'broken before' = EXPECTED; it must not be."""
    before = {"total::Valid SUMX": {"error": "untranslatable: unsupported function in: SUMX(...)"}}
    rows = classify(before, {}, {})
    assert rows[0]["class"] != "EXPECTED", rows[0]


def test_new_check_not_labelled_broken_before(tmp_path):
    """ADVERSARY: a check that did not exist before is a new object, not 'was broken before, resolves after'."""
    rows = classify({}, {"total::New": {"value": 1.0}}, {"total::New": {"value": 1.0}})
    assert "broken" not in rows[0]["reason"], rows[0]


def test_fresh_data_without_tolerance_on_noise(tmp_path):
    """ADVERSARY: value None (blank) before vs 0 after on frozen data is a real change, not IDENTICAL/EXPECTED."""
    rows = classify({"total::X": {"value": None}}, {"total::X": {"value": 0.0}}, {"total::X": {"value": 0.0}})
    assert rows[0]["class"] == "UNEXPECTED", rows[0]


# ---------- consumer check ----------

def test_consumer_check_unknown_table_for_measure(tmp_path):
    """ADVERSARY: a visual measure bound to the wrong entity (table) is a broken binding."""
    m = load_model(make_model(tmp_path))
    r = make_reports(tmp_path, fields=(("Measure", "GhostTable", "Total Paid"),))
    res = consumer_check(m, report_fields(r))
    assert res["passed"] == 0, res
