"""ADVERSARY round 3: refusal bypasses, new feature readers, nested row context, rollback
marker tampering, the demo's self-acknowledged UNTESTED measure, README vs results/.
All tests use minimal synthetic TMDL (no demo re-runs) to stay fast."""
import json
import re
import shutil
from pathlib import Path

import duckdb
import pytest
from helpers import DIMDATE, DIMPOLICY, FACT, make_model, make_reports, removed_columns

from smguard.analyze import broken_measures
from smguard.tmdl import load_model
from smguard.trim import _same_tree, apply, plan, rollback

PROJECT = Path(__file__).resolve().parents[2]
UNSUPPORTED = re.compile(r"unsupported", re.I)
VIS = (("Measure", "FactClaims", "Total Paid"),)


def _kept_or_refused(m, r, col):
    """True if `col` is not planned for removal, or plan() refuses with 'unsupported feature present'."""
    try:
        p = plan(m, r)
    except Exception as e:  # noqa: BLE001
        return bool(UNSUPPORTED.search(str(e)))
    return col not in removed_columns(p)


# ---------- can something slip past the refusal? ----------

def test_role_inline_in_model_tmdl_is_read_or_refused(tmp_path):
    """ADVERSARY: an RLS role written inline in definition/model.tmdl (skipped file) must pin its column or refuse."""
    mt = ("model Model\n\tculture: en-US\n\n"
          "role Regional\n\tmodelPermission: read\n\n\ttablePermission DimPolicy = DimPolicy[Region] = \"North\"\n")
    m = make_model(tmp_path, extra={"definition/model.tmdl": mt})
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimPolicy", "Region"))


def test_table_inline_in_database_tmdl_is_read_or_refused(tmp_path):
    """ADVERSARY: a calculated table defined inside database.tmdl must not be silently ignored."""
    db = ("database Contoso\n\tcompatibilityLevel: 1600\n\n"
          "table Extra\n\tpartition Extra = calculated\n\t\tsource = VALUES(FactClaims[ClaimStatus])\n")
    m = make_model(tmp_path, extra={"definition/database.tmdl": db})
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ClaimStatus"))


def test_detail_rows_definition_column_kept(tmp_path):
    """ADVERSARY: a column used only in a measure's detailRowsDefinition (drill-through) is kept."""
    fact = FACT.replace("\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n",
                        "\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n"
                        "\t\tdetailRowsDefinition = SELECTCOLUMNS(FactClaims, \"Status\", FactClaims[ClaimStatus])\n")
    m = make_model(tmp_path, fact=fact)
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ClaimStatus"))


def test_space_indented_column_is_read_or_refused(tmp_path):
    """ADVERSARY: a column block indented with spaces instead of tabs is either read or the trim refuses."""
    fact = FACT + "\n    column Spacey\n        dataType: string\n        sourceColumn: Spacey\n"
    fact += "\n\tmeasure S = COUNTROWS(FILTER(FactClaims, FactClaims[Spacey] = \"x\"))\n"
    m = make_model(tmp_path, fact=fact)
    r = make_reports(tmp_path, fields=(("Measure", "FactClaims", "S"),))
    try:
        p = plan(m, r)
    except Exception as e:  # noqa: BLE001
        assert UNSUPPORTED.search(str(e))
        return
    assert "S" not in {x["measure"] for x in p["remove_measures"] + p["blocked_measures"]}


def test_mixed_tab_space_property_is_read_or_refused(tmp_path):
    """ADVERSARY: a sortByColumn line indented with tab+spaces is either honoured or the trim refuses."""
    dd = DIMDATE.replace("\t\tsourceColumn: MonthName\n", "\t\tsourceColumn: MonthName\n\t    sortByColumn: Month\n")
    m = make_model(tmp_path, dimdate=dd)
    r = make_reports(tmp_path, fields=VIS + (("Column", "DimDate", "MonthName"),))
    assert _kept_or_refused(m, r, ("DimDate", "Month"))


def test_property_nested_deeper_is_read_or_refused(tmp_path):
    """ADVERSARY: a sortByColumn property indented one level too deep is honoured or refused, not dropped."""
    dd = DIMDATE.replace("\t\tsourceColumn: MonthName\n", "\t\tsourceColumn: MonthName\n\t\t\tsortByColumn: Month\n")
    m = make_model(tmp_path, dimdate=dd)
    r = make_reports(tmp_path, fields=VIS + (("Column", "DimDate", "MonthName"),))
    assert _kept_or_refused(m, r, ("DimDate", "Month"))


def test_multiline_m_with_hash_quoted_name_keeps_column(tmp_path):
    """ADVERSARY: multi-line M that references a column as #"Claim Status" keeps that model column (or refuses)."""
    fact = FACT.replace("column ClaimStatus", "column 'Claim Status'").replace("sourceColumn: ClaimStatus", "sourceColumn: Claim Status")
    fact += ("\n\tpartition FactClaims = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
             "\t\t\t\t\tS = Parquet.Document(File.Contents(\"x\")),\n"
             "\t\t\t\t\tF = Table.SelectRows(S, each #\"Claim Status\" <> \"Void\")\n\t\t\t\tin\n\t\t\t\t\tF\n")
    m = make_model(tmp_path, fact=fact)
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "Claim Status"))


def test_m_partition_source_at_wrong_depth_refuses(tmp_path):
    """ADVERSARY: M code lines written at the property depth (not deeper) must not be silently skipped."""
    fact = FACT + ("\n\tpartition FactClaims = m\n\t\tmode: import\n\t\tsource =\n"
                   "\t\tlet S = Table.SelectRows(X, each [ClaimStatus] <> \"Void\") in S\n")
    m = make_model(tmp_path, fact=fact)
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ClaimStatus"))


def test_unknown_keyword_inside_measure_body_refuses_or_is_dax(tmp_path):
    """ADVERSARY: an unknown object nested under a measure (e.g. kpi) is refused, not absorbed."""
    fact = FACT.replace("\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n",
                        "\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n"
                        "\t\tkpi\n\t\t\ttargetExpression = SUM(FactClaims[ReserveAmount])\n")
    m = make_model(tmp_path, fact=fact)
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ReserveAmount"))


# ---------- new feature readers ----------

def test_calc_item_multiline_body_column_kept(tmp_path):
    """ADVERSARY: a calculation item with a multi-line body keeps the column it uses (or refuses)."""
    cg = ("table 'Time Calc'\n\tcalculationGroup\n\n\t\tcalculationItem H1 =\n"
          "\t\t\t\tCALCULATE(SELECTEDMEASURE(),\n\t\t\t\t\tDimDate[Month] <= 6)\n\n"
          "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n")
    m = make_model(tmp_path, extra={"definition/tables/Time Calc.tmdl": cg})
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimDate", "Month"))


def test_calc_group_precedence_property_not_false_refusal(tmp_path):
    """ADVERSARY: a standard calculationGroup with `precedence:` is supported, not refused (false refusal)."""
    cg = ("table 'Time Calc'\n\tcalculationGroup\n\t\tprecedence: 1\n\n\t\tcalculationItem All = SELECTEDMEASURE()\n\n"
          "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n")
    m = make_model(tmp_path, extra={"definition/tables/Time Calc.tmdl": cg})
    plan(m, make_reports(tmp_path))


def test_perspective_hierarchy_pins_its_level_columns(tmp_path):
    """ADVERSARY: a perspectiveHierarchy whose levels are the only use of a column keeps that column."""
    dd = DIMDATE + "\n\thierarchy Calendar\n\t\tlevel Year\n\t\t\tcolumn: Year\n\n\t\tlevel Month\n\t\t\tcolumn: Month\n"
    persp = "perspective Sales\n\tperspectiveTable DimDate\n\t\tperspectiveHierarchy Calendar\n"
    m = make_model(tmp_path, dimdate=dd, extra={"definition/perspectives/Sales.tmdl": persp})
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimDate", "Month"))


def test_perspective_measure_on_broken_measure_not_deleted_silently(tmp_path):
    """ADVERSARY: deleting a broken measure listed in a perspective leaves a dangling perspectiveMeasure; block or refuse."""
    fact = FACT + "\n\tmeasure Ghost = SUM(FactClaims[Nope])\n"
    persp = "perspective Sales\n\tperspectiveTable FactClaims\n\t\tperspectiveMeasure Ghost\n"
    m = make_model(tmp_path, fact=fact, extra={"definition/perspectives/Sales.tmdl": persp})
    try:
        p = plan(m, make_reports(tmp_path))
    except Exception as e:  # noqa: BLE001
        assert UNSUPPORTED.search(str(e))
        return
    assert "Ghost" not in {x["measure"] for x in p["remove_measures"]}


def test_ols_on_quoted_names_with_spaces(tmp_path):
    """ADVERSARY: OLS columnPermission on 'Table With Space'.'Column With Space' pins the column."""
    pol = (DIMPOLICY.replace("table DimPolicy", "table 'Dim Policy'").replace("column Region", "column 'Sales Region'"))
    rels = "relationship rel1\n\tfromColumn: FactClaims.PolicyKey\n\ttoColumn: 'Dim Policy'.PolicyKey\n"
    role = "role Restricted\n\tmodelPermission: read\n\n\ttablePermission 'Dim Policy'\n\t\tcolumnPermission 'Sales Region' = none\n"
    m = make_model(tmp_path, dimpolicy=pol, rels=rels, extra={"definition/roles/R.tmdl": role})
    assert _kept_or_refused(m, make_reports(tmp_path), ("Dim Policy", "Sales Region"))


def test_nameof_unqualified_or_unquoted(tmp_path):
    """ADVERSARY: NAMEOF(FactClaims[ReserveAmount]) without quotes, inside a multi-line field parameter, keeps the column."""
    fp = ("table Metric\n\tcolumn Metric\n\t\tdataType: string\n\t\tsourceColumn: [Value1]\n\n"
          "\tpartition Metric = calculated\n\t\tmode: import\n\t\tsource =\n\t\t\t\t{\n"
          "\t\t\t\t\t(\"Paid\", NAMEOF([Total Paid]), 0),\n\t\t\t\t\t(\"Reserve\", NAMEOF(FactClaims[ReserveAmount]), 1)\n\t\t\t\t}\n")
    m = make_model(tmp_path, extra={"definition/tables/Metric.tmdl": fp})
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ReserveAmount"))


def test_format_string_definition_on_broken_measure_reference(tmp_path):
    """ADVERSARY: a formatStringDefinition that references a missing measure makes its measure broken."""
    fact = FACT.replace("\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n",
                        "\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n"
                        "\t\tformatStringDefinition = IF([Ghost Fmt] > 1, \"#,0\", \"0\")\n")
    assert "Total Paid" in broken_measures(load_model(make_model(tmp_path, fact=fact)))


# ---------- nested row-context scoping ----------

def _measure(tmp_path, expr):
    return load_model(make_model(tmp_path, fact=FACT + f"\n\tmeasure X = {expr}\n"))


def test_filter_over_all_table_unqualified_column_valid(tmp_path):
    """ADVERSARY: FILTER(ALL(FactClaims), [ReserveAmount] > 0) is valid DAX (row context from ALL(T)); not broken."""
    m = _measure(tmp_path, "COUNTROWS(FILTER(ALL(FactClaims), [ReserveAmount] > 0))")
    assert "X" not in broken_measures(m)


def test_sumx_over_relatedtable_unqualified_column_valid(tmp_path):
    """ADVERSARY: SUMX(DimPolicy, SUMX(RELATEDTABLE(FactClaims), [ReserveAmount])) is valid; not broken."""
    m = _measure(tmp_path, "SUMX(DimPolicy, SUMX(RELATEDTABLE(FactClaims), [ReserveAmount]))")
    assert "X" not in broken_measures(m)


def test_unqualified_column_out_of_scope_is_broken(tmp_path):
    """ADVERSARY: [ReserveAmount] used in a DimPolicy row context is broken even if FactClaims is iterated elsewhere."""
    m = _measure(tmp_path, "SUMX(FactClaims, 1) + SUMX(DimPolicy, [ReserveAmount])")
    assert "X" in broken_measures(m)


def test_unqualified_column_outside_any_iterator_is_broken(tmp_path):
    """ADVERSARY: [ReserveAmount] after the SUMX has closed has no row context -> broken."""
    m = _measure(tmp_path, "SUMX(FactClaims, FactClaims[PaidAmount]) + [ReserveAmount]")
    assert "X" in broken_measures(m)


def test_var_shadowing_table_name_does_not_hide_missing_table(tmp_path):
    """ADVERSARY: a VAR named like a missing table only hides refs AFTER its definition; COUNTROWS(Ghost) before it is broken."""
    m = _measure(tmp_path, "COUNTROWS(Ghost) + (VAR Ghost = FactClaims RETURN COUNTROWS(Ghost))")
    assert "X" in broken_measures(m)


# ---------- rollback marker ----------

def _applied(tmp_path):
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    p = plan(m, r)
    apply(m, tmp_path, r, tmp_path / "work", p)
    return m


def _snap(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


def test_tampered_marker_cannot_redirect_rollback(tmp_path):
    """ADVERSARY: editing the marker's recorded model path must not let rollback overwrite another folder."""
    _applied(tmp_path)
    victim = make_model(tmp_path / "victim")
    snap = _snap(victim)
    mk = tmp_path / "work" / ".smguard-work"
    j = json.loads(mk.read_text())
    j["model"] = str(victim.resolve())
    mk.write_text(json.dumps(j))
    try:
        rollback(tmp_path / "work", victim)
    except Exception:
        pass
    assert _snap(victim) == snap


def test_tampered_backup_is_detected_on_rollback(tmp_path):
    """ADVERSARY: a backup edited after apply() must make rollback refuse (rollback must restore the ORIGINAL bytes)."""
    m = _applied(tmp_path)
    original = _snap(m)
    (tmp_path / "work" / "backup" / "definition" / "tables" / "FactClaims.tmdl").write_text("table FactClaims\n")
    try:
        rollback(tmp_path / "work", m)
    except Exception:
        pass
    assert _snap(m) == original


def test_missing_marker_refuses(tmp_path):
    """ADVERSARY: with the marker deleted, rollback refuses and the target is untouched."""
    m = _applied(tmp_path)
    snap = _snap(m)
    (m / "definition" / "tables" / "FactClaims.tmdl").write_text("damaged\n")
    snap = _snap(m)
    (tmp_path / "work" / ".smguard-work").unlink()
    with pytest.raises(Exception):
        rollback(tmp_path / "work", m)
    assert _snap(m) == snap


def test_marker_as_directory_refuses(tmp_path):
    """ADVERSARY: a marker that is a directory (not smguard's file) is rejected."""
    m = _applied(tmp_path)
    mk = tmp_path / "work" / ".smguard-work"
    mk.unlink()
    mk.mkdir()
    with pytest.raises(Exception):
        rollback(tmp_path / "work", m)


def test_foreign_marker_does_not_license_work_dir_deletion(tmp_path):
    """ADVERSARY: a folder with user files and an empty '.smguard-work' file planted is not wiped by apply()."""
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    wd = tmp_path / "userdir"
    wd.mkdir()
    (wd / "thesis.docx").write_text("precious")
    (wd / ".smguard-work").write_text("")
    try:
        apply(m, tmp_path, r, wd, plan(m, r))
    except Exception:
        pass
    assert (wd / "thesis.docx").exists()


# ---------- UNTESTED acknowledgement ----------

def test_untested_acknowledgement_is_not_hardcoded_in_code():
    """ADVERSARY: the human sign-off for UNTESTED measures must not live in tool code (self-acknowledgement)."""
    src = (PROJECT / "smguard" / "demo.py").read_text(encoding="utf-8")
    assert "ACKNOWLEDGED_UNTESTED = {" not in src


def test_untested_acknowledgement_is_a_reviewed_results_file():
    """ADVERSARY: acknowledgements live in results/acknowledgements.json, naming reviewer + date + measure + reason,
    and the README calls it a human sign-off."""
    f = PROJECT / "results" / "acknowledgements.json"
    assert f.is_file()
    acks = json.loads(f.read_text(encoding="utf-8"))
    items = acks if isinstance(acks, list) else acks.get("acknowledgements", [])
    assert items and all({"measure", "reviewer", "date", "reason"} <= set(i) for i in items)
    assert re.search(r"human sign-off", (PROJECT / "README.md").read_text(encoding="utf-8"), re.I)


def test_ack_only_covers_listed_measures():
    """ADVERSARY: summary.json acknowledged set equals the untested set exactly; safe is false otherwise."""
    s = json.loads((PROJECT / "results" / "summary.json").read_text())
    for env, e in s["envs"].items():
        assert set(e.get("untested_acknowledged", {})) == set(e.get("untested_measures", [])), env
        if e["unexpected"] or set(e.get("untested_measures", [])) - set(e.get("untested_acknowledged", {})):
            assert e["safe"] is False, env


# ---------- README vs results ----------

def test_readme_table_and_untested_match_summary():
    """ADVERSARY: README table rows and per-env UNTESTED counts equal results/summary.json."""
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    s = json.loads((PROJECT / "results" / "summary.json").read_text())["envs"]
    for env, e in s.items():
        row = next(l for l in text.splitlines() if l.startswith(f"| {env} |"))
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[1] == str(e["est_mb_before"]) and cells[2] == str(e["est_mb_after"]), env
        assert cells[4] == f"{e['columns_before']} -> {e['columns_after']}", env
        assert cells[5] == f"{e['measures_before']} -> {e['measures_after']}", env
        assert str(e["regression_checks"]) in cells and str(e["unexpected"]) in cells, env
        line = next(l for l in text.splitlines() if l.startswith(f"- {env}:"))
        assert f"{e['untested']} UNTESTED" in line, env


def test_readme_adversary_count_current():
    """ADVERSARY: README's adversary test count matches the files in tests/adversarial (counting rule of round 2)."""
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    n = 0
    for f in (PROJECT / "tests" / "adversarial").glob("test_*.py"):
        t = f.read_text(encoding="utf-8")
        n += len(re.findall(r"^def test_", t, re.M))
        for args in re.findall(r"@pytest.mark.parametrize\([^,]+,\s*\[([^\]]*)\]", t):
            n += args.count(",")
    assert f"{n} tests in `tests/adversarial/`" in text
