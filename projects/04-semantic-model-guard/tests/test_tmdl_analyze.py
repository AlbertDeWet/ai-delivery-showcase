import json

from smguard.analyze import analyze, broken_measures, drift, expr_refs, report_fields, consumer_check
from smguard.tmdl import Model, load_model, parse_table_file, remove_objects


def _model(text):
    m = Model("t", None)
    parse_table_file(text, m)
    return m


def test_parse_quoted_names():
    m = _model("table 'Fact X'\n\tmeasure 'My ''Q'' M' = SUM('Fact X'[Amt])\n\tcolumn 'Amt Col'\n\t\tdataType: double\n")
    assert m.tables == ["Fact X"]
    assert "My 'Q' M" in m.measures
    assert m.columns[("Fact X", "Amt Col")].props["dataType"] == "double"


def test_expr_refs_ignores_string_literals():
    cols, meas, _ = expr_refs('CALCULATE(SUM(F[A]), F[S] = "[NotAMeasure]") + [Real]')
    assert cols == {("F", "A"), ("F", "S")}
    assert meas == {"Real"}


def test_broken_direct_transitive_and_cycle():
    m = _model("table F\n\tmeasure A = SUM(F[x])\n\tmeasure B = [A] + 1\n\tmeasure C = [D]\n\tmeasure D = [C]\n"
               "\tmeasure E = SUM(F[y])\n\tcolumn y\n\t\tdataType: double\n")
    b = broken_measures(m)
    assert set(b) == {"A", "B", "C", "D"}
    assert "missing F[x]" in b["A"] and "depends on broken" in b["B"] and "circular" in b["C"]


def test_generated_counts(generated):
    m = load_model(generated / "models" / "DEV")
    assert sum(1 for t, _ in m.columns if t == "FactClaims") == 32
    assert len(broken_measures(m)) == 10


def test_unused_columns_exclude_keys_and_visual_fields(generated):
    r = analyze(generated / "models" / "DEV", generated / "data" / "DEV" / "v1", generated / "reports")
    unused = set(r["unused_columns"])
    assert "FactClaims[ClaimNarrative]" in unused
    for kept in ("FactClaims[PolicyKey]", "FactClaims[PaidAmount]", "FactClaims[ClaimStatus]",
                 "FactClaims[RecoveryAmount]", "DimDate[Year]", "DimPolicy[PolicyNumber]"):
        assert kept not in unused
    assert "ESTIMATE" in r["est_note"]


def test_size_estimate_ranks_text_columns_high(generated):
    r = analyze(generated / "models" / "DEV", generated / "data" / "DEV" / "v1", generated / "reports")
    top = [c["column"] for c in r["column_detail"][:3]]
    assert "ClaimNarrative" in top


def test_consumer_check_detects_missing_field(workspace, generated):
    f = next((workspace / "reports").rglob("kpi_paid/visual.json"))
    j = json.loads(f.read_text())
    j["visual"]["query"]["queryState"]["Values"]["projections"][0]["field"]["Measure"]["Property"] = "Nope"
    f.write_text(json.dumps(j))
    res = consumer_check(load_model(generated / "models" / "DEV"), report_fields(workspace / "reports"))
    assert res["passed"] == res["reports"] - 1
    assert "[Nope] missing" in res["failures"][0][2]


def test_drift_detects_planted_differences(generated):
    d = drift({e: generated / "models" / e for e in ("DEV", "UAT", "PROD")})
    objs = {x["object"] for k in ("columns", "measures", "expressions") for x in d[k]}
    assert {"FactClaims[TempFixFlag]", "[Commission Ratio]", "[Reopen Paid]", "[Avg Paid per Claim]"} <= objs


def test_remove_objects_only_removes_targets(workspace):
    p = workspace / "models" / "DEV"
    before = load_model(p)
    remove_objects(p, {("FactClaims", "AdjusterNote")}, {"Broker Paid"})
    after = load_model(p)
    assert set(before.columns) - set(after.columns) == {("FactClaims", "AdjusterNote")}
    assert set(before.measures) - set(after.measures) == {"Broker Paid"}
    assert after.columns[("FactClaims", "SourceRowHash")].props == before.columns[("FactClaims", "SourceRowHash")].props
