"""ADVERSARY round 2: attacks on the Round 1 fixes (new parser, UNTESTED grading,
work_dir safety, byte-exact removal, report bindings, README numbers)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import DIMPOLICY, FACT, make_model, make_reports, removed_columns

from smguard.analyze import broken_measures, report_fields
from smguard.regress import classify
from smguard.tmdl import load_model
from smguard.trim import apply, plan, rollback

PROJECT = Path(__file__).resolve().parents[2]
UNSUPPORTED = re.compile(r"unsupported", re.I)


def _kept_or_refused(m, r, col):
    """True if `col` is not planned for removal, or plan() refuses with 'unsupported feature present'."""
    try:
        p = plan(m, r)
    except Exception as e:  # noqa: BLE001
        return bool(UNSUPPORTED.search(str(e)))
    return col not in removed_columns(p)


# ---------- parser: DAX forms ----------

def test_var_table_variable_is_not_a_missing_table(tmp_path):
    """ADVERSARY: VAR t = FILTER(...) RETURN COUNTROWS(t) is valid DAX; t is a variable, not a missing table."""
    fact = FACT + ('\n\tmeasure OpenCnt =\n\t\t\tVAR t = FILTER(FactClaims, FactClaims[ClaimStatus] = "Open")\n'
                   "\t\t\tRETURN COUNTROWS(t)\n")
    assert "OpenCnt" not in broken_measures(load_model(make_model(tmp_path, fact=fact)))


def test_unqualified_column_in_row_context_is_valid(tmp_path):
    """ADVERSARY: SUMX(FactClaims, [ReserveAmount]) - an unqualified column in row context - is valid and keeps the column."""
    fact = FACT + "\n\tmeasure R = SUMX(FactClaims, [ReserveAmount])\n"
    m = make_model(tmp_path, fact=fact)
    assert "R" not in broken_measures(load_model(m))
    assert ("FactClaims", "ReserveAmount") not in removed_columns(plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "R"),))))


def test_double_dash_inside_column_name_is_not_a_comment(tmp_path):
    """ADVERSARY: '--' inside a [column name] is not a DAX comment; the reference survives and the column is kept."""
    fact = FACT.replace("column ReserveAmount", "column 'Reserve -- Net'") + "\n\tmeasure R = SUM(FactClaims[Reserve -- Net])\n"
    m = make_model(tmp_path, fact=fact)
    assert "R" not in broken_measures(load_model(m))
    assert ("FactClaims", "Reserve -- Net") not in removed_columns(plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "R"),))))


def test_double_slash_inside_quoted_table_is_not_a_comment(tmp_path):
    """ADVERSARY: '//' inside a 'quoted table name' is not a comment."""
    fact = (FACT.replace("table FactClaims", "table 'Claims//Fact'").replace("FactClaims[PaidAmount]", "'Claims//Fact'[PaidAmount]"))
    m = load_model(make_model(tmp_path, fact=fact, rels=""))
    assert broken_measures(m) == {}


def test_string_literal_with_brackets_and_nested_parens(tmp_path):
    """ADVERSARY: a string literal containing [Ghost] and nested parentheses does not break a valid measure."""
    fact = FACT + ('\n\tmeasure L = IF(DIVIDE((SUM(FactClaims[ReserveAmount]) * (1 + 2)), 3) > 0, "[Ghost] (x)", "y")\n')
    m = make_model(tmp_path, fact=fact)
    assert "L" not in broken_measures(load_model(m))
    assert ("FactClaims", "ReserveAmount") not in removed_columns(plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "L"),))))


def test_treatas_userelationship_columns_kept(tmp_path):
    """ADVERSARY: columns used only inside TREATAS / USERELATIONSHIP are kept."""
    fact = FACT + ("\n\tmeasure T = CALCULATE([Total Paid], TREATAS(VALUES(DimPolicy[Region]), DimPolicy[LineOfBusiness]), "
                   "USERELATIONSHIP(FactClaims[ReserveAmount], DimDate[Month]))\n")
    m = make_model(tmp_path, fact=fact)
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "T"),)))
    assert not {("DimPolicy", "Region"), ("FactClaims", "ReserveAmount"), ("DimDate", "Month")} & removed_columns(p)


def test_dynamic_format_string_column_kept(tmp_path):
    """ADVERSARY: a column used only in a measure's formatStringDefinition (dynamic format string) is kept."""
    fact = FACT.replace("\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n",
                        "\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n"
                        "\t\tformatStringDefinition = IF(SELECTEDVALUE(DimPolicy[Region]) = \"North\", \"#,0\", \"#,0.00\")\n")
    m = make_model(tmp_path, fact=fact)
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimPolicy", "Region"))


# ---------- parser: model features the builder lists as open ----------

def test_calculation_group_column_kept_or_refused(tmp_path):
    """ADVERSARY: a column used only in a calculation item is kept, or the trim refuses (unsupported feature)."""
    cg = ("table 'Time Calc'\n\tcalculationGroup\n\n\t\tcalculationItem H1 = CALCULATE(SELECTEDMEASURE(), DimDate[Month] <= 6)\n\n"
          "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n")
    m = make_model(tmp_path, extra={"definition/tables/Time Calc.tmdl": cg})
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimDate", "Month"))


def test_perspective_column_kept_or_refused(tmp_path):
    """ADVERSARY: removing a column a perspective lists leaves an invalid model; keep it or refuse."""
    persp = "perspective Sales\n\tperspectiveTable DimPolicy\n\t\tperspectiveColumn Region\n"
    m = make_model(tmp_path, extra={"definition/perspectives/Sales.tmdl": persp})
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimPolicy", "Region"))


def test_ols_column_permission_kept_or_refused(tmp_path):
    """ADVERSARY: a column named in object-level security (columnPermission) is kept, or the trim refuses."""
    role = "role Restricted\n\tmodelPermission: read\n\n\ttablePermission DimPolicy\n\t\tcolumnPermission Region = none\n"
    m = make_model(tmp_path, extra={"definition/roles/Restricted.tmdl": role})
    assert _kept_or_refused(m, make_reports(tmp_path), ("DimPolicy", "Region"))


def test_field_parameter_column_kept_or_refused(tmp_path):
    """ADVERSARY: a column named only in a field parameter (NAMEOF in a calculated partition) is kept, or refused."""
    fp = ("table Metric\n\tcolumn Metric\n\t\tdataType: string\n\t\tsourceColumn: [Value1]\n\n"
          "\tpartition Metric = calculated\n\t\tmode: import\n"
          "\t\tsource = {(\"Reserve\", NAMEOF('FactClaims'[ReserveAmount]), 0)}\n")
    m = make_model(tmp_path, extra={"definition/tables/Metric.tmdl": fp})
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ReserveAmount"))


def test_calculated_table_column_kept_or_refused(tmp_path):
    """ADVERSARY: a column used only by a calculated table's DAX source is kept, or refused."""
    ct = ("table OpenClaims\n\tcolumn ClaimStatus\n\t\tdataType: string\n\t\tsourceColumn: [ClaimStatus]\n\n"
          "\tpartition OpenClaims = calculated\n\t\tmode: import\n"
          "\t\tsource = FILTER(VALUES(FactClaims[ClaimStatus]), FactClaims[ClaimStatus] <> \"Void\")\n")
    m = make_model(tmp_path, extra={"definition/tables/OpenClaims.tmdl": ct})
    r = make_reports(tmp_path, fields=(("Measure", "FactClaims", "Total Paid"), ("Column", "OpenClaims", "ClaimStatus")))
    assert _kept_or_refused(m, r, ("FactClaims", "ClaimStatus"))


def test_power_query_partition_column_kept_or_refused(tmp_path):
    """ADVERSARY: a column the table's own M partition references by name is kept, or the trim refuses."""
    fact = FACT + ("\n\tpartition FactClaims = m\n\t\tmode: import\n\t\tsource =\n\t\t\t\tlet\n"
                   "\t\t\t\t\tS = Parquet.Document(File.Contents(\"x\")),\n"
                   "\t\t\t\t\tF = Table.SelectRows(S, each [ClaimStatus] <> \"Void\")\n\t\t\t\tin\n\t\t\t\t\tF\n")
    m = make_model(tmp_path, fact=fact)
    assert _kept_or_refused(m, make_reports(tmp_path), ("FactClaims", "ClaimStatus"))


# ---------- grader: UNTESTED cannot be a hiding place ----------

def test_untested_definition_change_is_unexpected():
    """ADVERSARY: an untranslatable measure whose DAX definition changed must be UNEXPECTED, not UNTESTED."""
    b = {"total::M": {"error": "untranslatable: SUMX", "def": "SUMX(FactClaims, FactClaims[A])"}}
    a = {"total::M": {"error": "untranslatable: SUMX", "def": "SUMX(FactClaims, FactClaims[B])"}}
    assert classify(b, a, a)[0]["class"] == "UNEXPECTED"


def test_untested_that_becomes_broken_is_unexpected():
    """ADVERSARY: a valid-but-untested measure that is BROKEN after the trim must be UNEXPECTED, not UNTESTED."""
    b = {"total::M": {"error": "untranslatable: SUMX", "def": "X"}}
    a = {"total::M": {"error": "broken: missing FactClaims[A]", "broken": True, "def": "X"}}
    assert classify(b, a, a)[0]["class"] == "UNEXPECTED"


def test_untested_disappearing_in_only_one_run_is_unexpected():
    """ADVERSARY: untested check present on old data but missing in the after run is UNEXPECTED."""
    b = {"total::M": {"error": "untranslatable: SUMX", "def": "X"}}
    assert classify(b, b, {})[0]["class"] == "UNEXPECTED"


def test_broken_flag_without_def_change_cannot_launder_valid_before():
    """ADVERSARY: a check valid before that is 'broken' after is UNEXPECTED (broken flag only excuses the BEFORE side)."""
    b = {"total::M": {"value": 1.0, "def": "X"}}
    a = {"total::M": {"error": "broken: missing", "broken": True, "def": "X"}}
    assert classify(b, a, a)[0]["class"] == "UNEXPECTED"


def test_demo_not_safe_with_untested_checks():
    """ADVERSARY: an env with UNTESTED checks must not be reported 'safe' with headline 'Unexpected 0' and nothing else.
    The summary must carry an explicit untested count next to unexpected."""
    s = json.loads((PROJECT / "results" / "summary.json").read_text())
    for env, e in s["envs"].items():
        if e["regression"].get("UNTESTED"):
            assert "untested" in e, env


# ---------- work_dir / rollback safety ----------

def _model_copy(tmp_path, generated):
    model = tmp_path / "Model"
    shutil.copytree(generated / "models" / "DEV", model)
    return model


def _snap(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


@pytest.mark.parametrize("how", ["dotdot", "case", "relative"])
def test_work_dir_overlap_tricks_refused(tmp_path, generated, how, monkeypatch):
    """ADVERSARY: work_dir inside the model via '..', different case or a relative path is refused and the model survives."""
    model = _model_copy(tmp_path, generated)
    snap = _snap(model)
    if how == "dotdot":
        wd = model / "definition" / ".." / "w"
    elif how == "case":
        import sys
        if sys.platform != "win32":
            pytest.skip("case-insensitive paths are a Windows concern")
        wd = Path(str(model).upper()) / "w"
    else:
        monkeypatch.chdir(model)
        wd = Path("w")
    p = plan(model, generated / "reports")
    with pytest.raises(ValueError):
        apply(model, generated / "data" / "DEV" / "v1", generated / "reports", wd, p)
    assert _snap(model) == snap


def _junction(link: Path, target: Path) -> bool:
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    except OSError:
        return False  # not Windows (e.g. CI on Linux): test is skipped
    return r.returncode == 0 and link.exists()


def test_work_dir_via_junction_refused(tmp_path, generated):
    """ADVERSARY: a work_dir reached through a directory junction pointing into the model is refused."""
    model = _model_copy(tmp_path, generated)
    snap = _snap(model)
    if not _junction(tmp_path / "link", model):
        pytest.skip("cannot create junction")
    p = plan(model, generated / "reports")
    with pytest.raises(ValueError):
        apply(model, generated / "data" / "DEV" / "v1", generated / "reports", tmp_path / "link" / "w", p)
    assert _snap(model) == snap


def test_model_via_junction_and_work_dir_real_path_refused(tmp_path, generated):
    """ADVERSARY: model passed through a junction, work_dir given as the real path inside it - refused."""
    model = _model_copy(tmp_path, generated)
    snap = _snap(model)
    if not _junction(tmp_path / "link", model):
        pytest.skip("cannot create junction")
    p = plan(tmp_path / "link", generated / "reports")
    with pytest.raises(ValueError):
        apply(tmp_path / "link", generated / "data" / "DEV" / "v1", generated / "reports", model / "w", p)
    assert _snap(model) == snap


def test_rollback_refuses_non_model_target(tmp_path, generated):
    """ADVERSARY: rollback onto an existing folder that is not a model (user data) must refuse, not delete it."""
    model = _model_copy(tmp_path, generated)
    p = plan(model, generated / "reports")
    apply(model, generated / "data" / "DEV" / "v1", generated / "reports", tmp_path / "work", p)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me")
    try:
        rollback(tmp_path / "work", victim)
    except Exception:
        pass
    assert (victim / "important.txt").exists()


def test_rollback_does_not_delete_preexisting_staging_folder(tmp_path, generated):
    """ADVERSARY: rollback must not silently delete a pre-existing '<target>.smguard-restore' folder it did not create."""
    model = _model_copy(tmp_path, generated)
    p = plan(model, generated / "reports")
    apply(model, generated / "data" / "DEV" / "v1", generated / "reports", tmp_path / "work", p)
    other = tmp_path / "Model.smguard-restore"
    other.mkdir()
    (other / "notes.txt").write_text("user file")
    try:
        rollback(tmp_path / "work", model)
    except Exception:
        pass
    assert (other / "notes.txt").exists()


# ---------- byte-exact removal ----------

def _apply_small(tmp_path, fact_text: str):
    m = make_model(tmp_path)
    (m / "definition" / "tables" / "FactClaims.tmdl").write_bytes(fact_text.encode("utf-8"))  # exact bytes
    r = make_reports(tmp_path, fields=(("Measure", "FactClaims", "Total Paid"), ("Column", "FactClaims", "ClaimStatus"),
                                       ("Column", "DimPolicy", "LineOfBusiness"), ("Column", "DimPolicy", "Region"),
                                       ("Column", "DimDate", "Year"), ("Column", "DimDate", "Month"),
                                       ("Column", "DimDate", "MonthName")))
    p = plan(m, r)
    assert removed_columns(p) == {("FactClaims", "ReserveAmount")}, removed_columns(p)
    out = apply(m, tmp_path, r, tmp_path / "work", p)
    return (Path(out["trimmed"]) / "definition" / "tables" / "FactClaims.tmdl").read_bytes(), out


RES_BLOCK = "\tcolumn ReserveAmount\n\t\tdataType: double\n\t\tsourceColumn: ReserveAmount\n\n"


def test_mid_file_removal_is_byte_exact_crlf(tmp_path):
    """ADVERSARY: removing a mid-file column from a CRLF file removes exactly that block; every other byte unchanged."""
    text = FACT.replace("\n", "\r\n")
    got, out = _apply_small(tmp_path, text)
    assert got == text.replace(RES_BLOCK.replace("\n", "\r\n"), "").encode("utf-8")
    assert out["safe"], out["checks"]


def test_mid_file_removal_takes_its_doc_comment(tmp_path):
    """ADVERSARY: a '///' description above a removed column is removed with it, not re-attached to the next column."""
    text = FACT.replace(RES_BLOCK, "\t/// Case reserve at valuation date\n" + RES_BLOCK)
    got, _ = _apply_small(tmp_path, text)
    assert got == FACT.replace(RES_BLOCK, "").encode("utf-8")


def test_removal_in_bom_file(tmp_path):
    """ADVERSARY: a TMDL file saved with a UTF-8 BOM is trimmed like any other; BOM and other bytes kept."""
    text = "﻿" + FACT
    got, out = _apply_small(tmp_path, text)
    assert got == ("﻿" + FACT.replace(RES_BLOCK, "")).encode("utf-8")
    assert out["checks"]["planned_columns_gone"]


def test_mid_file_removal_keeps_trailing_annotation_of_previous(tmp_path):
    """ADVERSARY: an annotation of the PREVIOUS column directly above the removed one is not swallowed."""
    text = FACT.replace(RES_BLOCK, "").replace(
        "\t\tsourceColumn: PaidAmount\n\n",
        "\t\tsourceColumn: PaidAmount\n\n\t\tannotation SummarizationSetBy = Automatic\n\n" + RES_BLOCK)
    got, _ = _apply_small(tmp_path, text)
    assert b"annotation SummarizationSetBy" in got


# ---------- report bookmarks and visual calculations ----------

def test_bookmark_alias_filter_column_kept(tmp_path):
    """ADVERSARY: a bookmark filter using a From/Source alias (no Entity) still pins its column."""
    m = make_model(tmp_path)
    r = make_reports(tmp_path)
    bm = {"explorationState": {"sections": {"p1": {"filters": {"byExpr": [{"filter": {
        "Version": 2, "From": [{"Name": "d", "Entity": "DimPolicy", "Type": 0}],
        "Where": [{"Condition": {"In": {"Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "d"}},
                                                                   "Property": "Region"}}],
                                        "Values": [[{"Literal": {"Value": "'North'"}}]]}}}]}}]}}}}}
    d = r / "R1" / "definition" / "bookmarks"
    d.mkdir(parents=True)
    (d / "b1.bookmark.json").write_text(json.dumps(bm))
    assert ("DimPolicy", "Region") not in removed_columns(plan(m, r))


def test_bookmark_entity_filter_column_kept(tmp_path):
    """ADVERSARY: a bookmark filter bound with Entity pins its column."""
    m = make_model(tmp_path)
    r = make_reports(tmp_path)
    bm = {"explorationState": {"filters": {"byExpr": [{"expression": {"Column": {
        "Expression": {"SourceRef": {"Entity": "DimPolicy"}}, "Property": "Region"}}}]}}}
    d = r / "R1" / "definition" / "bookmarks"
    d.mkdir(parents=True)
    (d / "b1.bookmark.json").write_text(json.dumps(bm))
    assert ("DimPolicy", "Region") not in removed_columns(plan(m, r))


def test_visual_calculation_does_not_create_false_binding(tmp_path):
    """ADVERSARY: a visual calculation (NativeVisualCalculation) is not reported as a missing model field."""
    m = make_model(tmp_path)
    r = make_reports(tmp_path)
    f = next(r.rglob("visual.json"))
    j = json.loads(f.read_text())
    j["visual"]["query"]["queryState"]["Values"]["projections"].append(
        {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": "RUNNINGSUM([Total Paid])",
                                               "Name": "Running Paid"}}, "queryRef": "Running Paid"})
    f.write_text(json.dumps(j))
    from smguard.analyze import consumer_check
    assert consumer_check(load_model(m), report_fields(r))["passed"] == 1


def test_visual_calculation_measure_ref_counts_as_use(tmp_path):
    """ADVERSARY: a broken measure referenced only from a visual calculation expression is blocked, not deleted."""
    fact = FACT + "\n\tmeasure Ghosty = SUM(FactClaims[Nope])\n"
    m = make_model(tmp_path, fact=fact)
    r = make_reports(tmp_path)
    f = next(r.rglob("visual.json"))
    j = json.loads(f.read_text())
    j["visual"]["query"]["queryState"]["Values"]["projections"].append(
        {"field": {"NativeVisualCalculation": {"Language": "dax", "Expression": "[Ghosty] * 2", "Name": "VC"}}})
    f.write_text(json.dumps(j))
    p = plan(m, r)
    assert "Ghosty" not in {x["measure"] for x in p["remove_measures"]}


# ---------- README vs results ----------

def test_readme_regression_prose_matches_summary():
    """ADVERSARY: every per-env classification count in README prose equals results/summary.json."""
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    s = json.loads((PROJECT / "results" / "summary.json").read_text())["envs"]
    for env, e in s.items():
        line = next(l for l in text.splitlines() if l.startswith(f"- {env}:"))
        rg = e["regression"]
        assert f"{rg.get('IDENTICAL')} identical" in line, env
        assert f"{rg.get('UNTESTED')} UNTESTED" in line, env
        assert f"{rg.get('EXPECTED: broken before; removed by trim')} expected" in line, env
        if rg.get("EXPECTED: fresh data"):
            assert f"{rg['EXPECTED: fresh data']} expected as fresh data" in line, env
        assert sum(rg.values()) == e["regression_checks"], env


def _count_tests(files):
    n = 0
    for f in files:
        t = f.read_text(encoding="utf-8")
        n += len(re.findall(r"^def test_", t, re.M))
        for args in re.findall(r"@pytest.mark.parametrize\([^,]+,\s*\[([^\]]*)\]", t):
            n += args.count(",")  # extra cases beyond the first
    return n


def test_readme_test_counts_match_repo():
    """ADVERSARY: README's builder and adversary test counts equal the number of collected tests."""
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    py = PROJECT / ".venv" / "Scripts" / "python.exe"

    def count(path):
        out = subprocess.run([str(py), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", path],
                             cwd=PROJECT, capture_output=True, text=True).stdout
        return sum(1 for l in out.splitlines() if "::" in l)
    b = _count_tests((PROJECT / "tests").glob("test_*.py"))
    a = _count_tests((PROJECT / "tests" / "adversarial").glob("test_*.py"))
    assert f"{b} tests in `tests/`" in text
    assert f"{a} tests in `tests/adversarial/`" in text
