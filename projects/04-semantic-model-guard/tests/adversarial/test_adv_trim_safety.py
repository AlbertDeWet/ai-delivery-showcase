"""ADVERSARY round 1: columns that are used, but not by a measure/relationship/visual
projection, must NOT be trimmed."""
from helpers import DIMDATE, DIMPOLICY, FACT, make_model, make_reports, removed_columns

from smguard.analyze import broken_measures
from smguard.tmdl import load_model
from smguard.trim import plan


def test_rls_only_column_not_trimmed(tmp_path):
    """ADVERSARY: a column referenced only by an RLS role filter (definition/roles/*.tmdl) is kept."""
    role = ("role 'Regional Managers'\n\tmodelPermission: read\n\n"
            "\ttablePermission DimPolicy = DimPolicy[Region] = \"North\"\n")
    m = make_model(tmp_path, extra={"definition/roles/Regional Managers.tmdl": role})
    p = plan(m, make_reports(tmp_path))
    assert ("DimPolicy", "Region") not in removed_columns(p)


def test_hierarchy_level_column_not_trimmed(tmp_path):
    """ADVERSARY: a column used only as a hierarchy level is kept."""
    dd = DIMDATE + "\n\thierarchy Calendar\n\t\tlevel Year\n\t\t\tcolumn: Year\n\n\t\tlevel Month\n\t\t\tcolumn: Month\n"
    m = make_model(tmp_path, dimdate=dd)
    p = plan(m, make_reports(tmp_path))
    assert ("DimDate", "Month") not in removed_columns(p)


def test_sort_by_column_target_not_trimmed(tmp_path):
    """ADVERSARY: the sortByColumn target of a column a visual uses is kept (else the visual breaks)."""
    dd = DIMDATE.replace("\t\tsourceColumn: MonthName\n", "\t\tsourceColumn: MonthName\n\t\tsortByColumn: Month\n")
    m = make_model(tmp_path, dimdate=dd)
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "Total Paid"),
                                                ("Column", "DimDate", "MonthName"))))
    assert ("DimDate", "MonthName") not in removed_columns(p)
    assert ("DimDate", "Month") not in removed_columns(p)


def test_calculated_column_is_parsed(tmp_path):
    """ADVERSARY: a calculated column (`column X = <expr>`) is a column of the model, not silently dropped."""
    fact = FACT + "\n\tcolumn 'Net Amount' = FactClaims[PaidAmount] - FactClaims[ReserveAmount]\n\t\tdataType: double\n"
    m = make_model(tmp_path, fact=fact)
    assert ("FactClaims", "Net Amount") in load_model(m).columns


def test_calculated_column_source_not_trimmed(tmp_path):
    """ADVERSARY: a column feeding a calculated column that a visual uses is kept."""
    fact = FACT + "\n\tcolumn 'Net Amount' = FactClaims[PaidAmount] - FactClaims[ReserveAmount]\n\t\tdataType: double\n"
    m = make_model(tmp_path, fact=fact)
    p = plan(m, make_reports(tmp_path, fields=(("Column", "FactClaims", "Net Amount"),)))
    assert ("FactClaims", "ReserveAmount") not in removed_columns(p)


def test_multiline_measure_columns_not_trimmed(tmp_path):
    """ADVERSARY: a multi-line measure (expression on following lines) keeps the columns it uses."""
    fact = FACT.replace("\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n",
                        "\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n\n"
                        "\tmeasure 'Total Reserve' =\n\t\t\tSUM(FactClaims[ReserveAmount])\n\t\tformatString: #,0.00\n")
    m = make_model(tmp_path, fact=fact)
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "Total Reserve"),)))
    assert ("FactClaims", "ReserveAmount") not in removed_columns(p)


def test_multiline_measure_expression_parsed(tmp_path):
    """ADVERSARY: a ``` fenced multi-line measure is parsed with its real expression, not an empty/fence string."""
    fact = FACT + ("\n\tmeasure 'Reserve Check' = ```\n\t\t\tVAR r = SUM(FactClaims[ReserveAmount])\n"
                   "\t\t\tRETURN r\n\t\t\t```\n")
    m = make_model(tmp_path, fact=fact)
    assert "ReserveAmount" in load_model(m).measures["Reserve Check"].expression


def test_quoted_names_with_spaces_keep_column(tmp_path):
    """ADVERSARY: 'Table With Space'[Column With Space] references are resolved; the column is kept."""
    fact = (FACT.replace("table FactClaims", "table 'Fact Claims'")
            .replace("FactClaims[PaidAmount]", "'Fact Claims'[Paid Amount]")
            .replace("column PaidAmount", "column 'Paid Amount'"))
    m = make_model(tmp_path, fact=fact, rels="")
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "Fact Claims", "Total Paid"),)))
    assert ("Fact Claims", "Paid Amount") not in removed_columns(p)
    assert broken_measures(load_model(m)) == {}


def test_measure_chain_keeps_leaf_column(tmp_path):
    """ADVERSARY: a column used only at the bottom of a 3-deep measure chain is kept."""
    fact = FACT + ("\n\tmeasure A = SUM(FactClaims[ReserveAmount])\n\n\tmeasure B = [A] * 2\n\n"
                   "\tmeasure C = DIVIDE([B], [Total Paid])\n")
    m = make_model(tmp_path, fact=fact)
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "C"),)))
    assert ("FactClaims", "ReserveAmount") not in removed_columns(p)


def test_visual_filter_column_not_trimmed(tmp_path):
    """ADVERSARY: a column used only in a visual-level filter (filterConfig) is kept."""
    flt = {"filterConfig": {"filters": [{"field": {"Column": {"Expression": {"SourceRef": {"Entity": "FactClaims"}},
                                                                 "Property": "ClaimStatus"}}}]}}
    m = make_model(tmp_path)
    p = plan(m, make_reports(tmp_path, extra_visual=flt))
    assert ("FactClaims", "ClaimStatus") not in removed_columns(p)


def test_aggregation_projection_does_not_crash(tmp_path):
    """ADVERSARY: an implicit-measure (Aggregation) projection is read, and its column kept, without crashing."""
    m = make_model(tmp_path)
    r = make_reports(tmp_path)
    f = next(r.rglob("visual.json"))
    import json
    j = json.loads(f.read_text())
    j["visual"]["query"]["queryState"]["Values"]["projections"].append(
        {"field": {"Aggregation": {"Expression": {"Column": {"Expression": {"SourceRef": {"Entity": "FactClaims"}},
                                                             "Property": "ReserveAmount"}}, "Function": 0}}})
    f.write_text(json.dumps(j))
    p = plan(m, r)
    assert ("FactClaims", "ReserveAmount") not in removed_columns(p)


def test_report_level_filter_column_not_trimmed(tmp_path):
    """ADVERSARY: a column used only in a report-level filter (report.json filterConfig) is kept."""
    import json
    m = make_model(tmp_path)
    r = make_reports(tmp_path)
    (r / "R1" / "definition" / "report.json").write_text(json.dumps({"filterConfig": {"filters": [
        {"field": {"Column": {"Expression": {"SourceRef": {"Entity": "DimPolicy"}}, "Property": "LineOfBusiness"}}}]}}))
    p = plan(m, r)
    assert ("DimPolicy", "LineOfBusiness") not in removed_columns(p)
