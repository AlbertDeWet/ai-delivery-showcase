"""ADVERSARY round 4: usefulness (real-world-shaped PBIP skeleton must be accepted),
allowlist / dax.py attacks, forged marker, placeholder sign-off. Fast: minimal TMDL."""
import json
import re
import shutil
from pathlib import Path

import pytest
from helpers import FACT, make_model, make_reports, removed_columns

from smguard.analyze import broken_measures
from smguard.tmdl import load_model
from smguard.trim import apply, plan, rollback

PROJECT = Path(__file__).resolve().parents[2]
UNSUPPORTED = re.compile(r"unsupported", re.I)
VIS = (("Measure", "FactClaims", "Total Paid"),)

# ---- shapes follow the public TMDL / PBIP folder docs; synthetic content only ----
DATABASE = "database\n\tcompatibilityLevel: 1601\n"
MODEL = ("model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
         "\tsourceQueryCulture: en-US\n\tdataAccessOptions\n\t\tlegacyRedirects\n\t\treturnErrorValuesAsNull\n\n"
         "queryGroup 'Fact Tables'\n\n\tannotation PBI_QueryOrder = [\"FactClaims\"]\n\n"
         "annotation __PBI_TimeIntelligenceEnabled = 0\n\n"
         "annotation PBI_ProTooling = [\"DevMode\"]\n\n"
         "ref table FactClaims\nref table DimPolicy\nref table DimDate\n\nref culture en-US\n")
CULTURE = ("cultureInfo en-US\n\n\tlinguisticMetadata =\n\t\t\t{\n\t\t\t  \"Version\": \"1.0.0\",\n"
           "\t\t\t  \"Language\": \"en-US\"\n\t\t\t}\n\t\tcontentType: json\n")
PBISM = json.dumps({"version": "4.2", "settings": {}})
PLATFORM = json.dumps({"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
                       "metadata": {"type": "SemanticModel", "displayName": "Contoso Claims"},
                       "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"}})


def skeleton(tmp_path, model_tmdl=MODEL, extra=None, metadata=True):
    files = {"definition/database.tmdl": DATABASE, "definition/model.tmdl": model_tmdl,
             "definition/cultures/en-US.tmdl": CULTURE}
    if metadata:
        files.update({"definition.pbism": PBISM, ".platform": PLATFORM, "diagramLayout.json": "{}"})
    files.update(extra or {})
    return make_model(tmp_path, extra=files)


def _refuses(m, r):
    try:
        plan(m, r)
    except Exception as e:  # noqa: BLE001
        return bool(UNSUPPORTED.search(str(e)))
    return False


# ---------- 1. usefulness: a real-world-shaped skeleton is ACCEPTED ----------

def test_real_world_tmdl_skeleton_is_accepted(tmp_path):
    """ADVERSARY: database.tmdl + model.tmdl (culture, ref table, annotations, queryGroup) + cultures/en-US.tmdl
    is the normal shape of every PBIP model; the trim must plan it, not refuse it."""
    m = skeleton(tmp_path, metadata=False)
    plan(m, make_reports(tmp_path, fields=VIS))


def test_real_world_pbip_folder_with_metadata_files_is_accepted(tmp_path):
    """ADVERSARY: definition.pbism, .platform and diagramLayout.json next to definition/ do not cause a refusal."""
    m = skeleton(tmp_path)
    plan(m, make_reports(tmp_path, fields=VIS))


def test_real_world_skeleton_trim_still_removes_unused_column(tmp_path):
    """ADVERSARY: on the accepted skeleton the trim still does its job (unused ReserveAmount planned)."""
    m = skeleton(tmp_path)
    p = plan(m, make_reports(tmp_path, fields=VIS))
    assert ("FactClaims", "ReserveAmount") in removed_columns(p)


def test_skeleton_with_inline_role_in_model_tmdl_still_refused_or_read(tmp_path):
    """ADVERSARY: accepting model.tmdl must not reopen R3-1: an inline role there pins its column or refuses."""
    mt = MODEL + "\nrole Regional\n\tmodelPermission: read\n\n\ttablePermission DimPolicy = DimPolicy[Region] = \"North\"\n"
    m = skeleton(tmp_path, model_tmdl=mt)
    r = make_reports(tmp_path, fields=VIS)
    try:
        p = plan(m, r)
    except Exception as e:  # noqa: BLE001
        assert UNSUPPORTED.search(str(e))
        return
    assert ("DimPolicy", "Region") not in removed_columns(p)


def test_skeleton_with_shared_expression_refused(tmp_path):
    """ADVERSARY: a genuinely unsupported construct (shared M expression file) in a real skeleton is still refused."""
    ex = "expression Source = \"C:\\\\data\" meta [IsParameterQuery=true, Type=\"Text\"]\n"
    m = skeleton(tmp_path, extra={"definition/expressions.tmdl": ex})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_skeleton_with_ref_to_missing_table_refused(tmp_path):
    """ADVERSARY: model.tmdl `ref table Ghost` with no table file is an inconsistent model -> refuse."""
    m = skeleton(tmp_path, model_tmdl=MODEL + "ref table Ghost\n")
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_skeleton_with_dax_udf_function_refused(tmp_path):
    """ADVERSARY: a DAX user-defined function file (functions.tmdl) is refused."""
    fn = "function 'Ghost.Fn' = (x) => x * 2\n"
    m = skeleton(tmp_path, extra={"definition/functions.tmdl": fn})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


# ---------- 2. allowlist / dax.py ----------

def _m(tmp_path, *measures, **kw):
    fact = FACT + "".join(f"\n\tmeasure {x}\n" for x in measures)
    return load_model(make_model(tmp_path, fact=fact, **kw))


def test_uncertain_context_does_not_hide_out_of_scope_column(tmp_path):
    """ADVERSARY: SUMX(ALL(DimPolicy), [ReserveAmount]) - DimPolicy has no ReserveAmount; the 'uncertain' fallback
    to ANY table's column hides the break."""
    assert "X" in broken_measures(_m(tmp_path, "X = SUMX(ALL(DimPolicy), [ReserveAmount])"))


def test_inner_var_scope_does_not_leak(tmp_path):
    """ADVERSARY: a VAR defined inside a parenthesised inner scope does not hide COUNTROWS(Ghost) outside it."""
    m = _m(tmp_path, "X = (VAR Ghost = FactClaims RETURN COUNTROWS(Ghost)) + COUNTROWS(Ghost)")
    assert "X" in broken_measures(m)


def test_var_named_like_real_table_column_ref(tmp_path):
    """ADVERSARY: VAR DimPolicy = ... then DimPolicy[Ghost] - the variable shadows the table; but a missing
    column on the REAL table earlier in the expression is still broken."""
    m = _m(tmp_path, "X = SUM(DimPolicy[Ghost]) + (VAR DimPolicy = FactClaims RETURN 1)")
    assert "X" in broken_measures(m)


def test_unicode_unquoted_table_name(tmp_path):
    """ADVERSARY: a valid reference to a table with a non-ASCII name (Schäden[Betrag]) is not 'missing table'."""
    fact = (FACT.replace("table FactClaims", "table Schäden").replace("FactClaims[PaidAmount]", "Schäden[PaidAmount]"))
    rels = "relationship rel2\n\tfromColumn: Schäden.DateKey\n\ttoColumn: DimDate.DateKey\n"
    m = load_model(make_model(tmp_path, fact=fact, rels=rels))
    assert broken_measures(m) == {}


def test_unicode_bracket_column_name_kept(tmp_path):
    """ADVERSARY: a column named with non-ASCII characters used by a measure is kept."""
    fact = FACT.replace("column ReserveAmount", "column 'Réserve'") + "\n\tmeasure R = SUM(FactClaims[Réserve])\n"
    m = make_model(tmp_path, fact=fact)
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "R"),)))
    assert ("FactClaims", "Réserve") not in removed_columns(p)


def test_allowed_function_as_identifier_not_called(tmp_path):
    """ADVERSARY: a table literally named 'Values' (an allowed function name) used as COUNTROWS(Values) resolves to the table."""
    extra = {"definition/tables/Values.tmdl": "table Values\n\tcolumn V\n\t\tdataType: int64\n\t\tsourceColumn: V\n"}
    m = load_model(make_model(tmp_path, fact=FACT + "\n\tmeasure X = COUNTROWS('Values')\n", extra=extra))
    assert "X" not in broken_measures(m)


def test_iterator_table_argument_from_function_keeps_columns(tmp_path):
    """ADVERSARY: SUMX(FILTER(FactClaims, FactClaims[ClaimStatus] = \"Open\"), [ReserveAmount]) keeps both columns."""
    fact = FACT + '\n\tmeasure X = SUMX(FILTER(FactClaims, FactClaims[ClaimStatus] = "Open"), [ReserveAmount])\n'
    m = make_model(tmp_path, fact=fact)
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "X"),)))
    assert not {("FactClaims", "ClaimStatus"), ("FactClaims", "ReserveAmount")} & removed_columns(p)


def test_function_name_case_and_space_still_allowlisted(tmp_path):
    """ADVERSARY: an allowlisted function written `sum (` (lower case, space before paren) is not refused."""
    m = make_model(tmp_path, fact=FACT + "\n\tmeasure X = sum (FactClaims[ReserveAmount])\n")
    p = plan(m, make_reports(tmp_path, fields=(("Measure", "FactClaims", "X"),)))
    assert ("FactClaims", "ReserveAmount") not in removed_columns(p)


def test_non_allowlisted_function_hidden_after_comment_newline_refused(tmp_path):
    """ADVERSARY: a non-allowlisted function placed after a // comment on the previous line is still refused."""
    fact = FACT + "\n\tmeasure X =\n\t\t\t// harmless\n\t\t\tOFFSET(1, ALLSELECTED(DimDate[Year]))\n"
    assert _refuses(make_model(tmp_path, fact=fact), make_reports(tmp_path, fields=VIS))


def test_non_allowlisted_function_inside_calc_item_refused(tmp_path):
    """ADVERSARY: the DAX allowlist also applies to calculation items, RLS filters and calculated columns."""
    cg = ("table 'Time Calc'\n\tcalculationGroup\n\n\t\tcalculationItem W = WINDOW(1, ABS, 0, REL, ALLSELECTED(DimDate[Year]))\n\n"
          "\tcolumn Name\n\t\tdataType: string\n\t\tsourceColumn: Name\n")
    m = make_model(tmp_path, extra={"definition/tables/Time Calc.tmdl": cg})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_dotted_udf_call_refused(tmp_path):
    """ADVERSARY: a dotted user-defined function call (Lib.Fn(...)) in a measure is refused, not treated as allowlisted."""
    m = make_model(tmp_path, fact=FACT + "\n\tmeasure X = Lib.Fn(FactClaims[ReserveAmount])\n")
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


# ---------- marker forged by rewriting all fields ----------

def _applied(tmp_path):
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    apply(m, tmp_path, r, tmp_path / "work", plan(m, r))
    return m, r


def _snap(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


def test_copied_marker_does_not_license_wiping_user_folder(tmp_path):
    """ADVERSARY: copying a genuine smguard marker into a folder of user files does not let apply() wipe it."""
    m, r = _applied(tmp_path)
    user = tmp_path / "user"
    user.mkdir()
    (user / "notes.docx").write_text("precious")
    shutil.copy(tmp_path / "work" / ".smguard-work", user / ".smguard-work")
    try:
        apply(m, tmp_path, r, user, plan(m, r))
    except Exception:
        pass
    assert (user / "notes.docx").exists()


def test_fully_forged_marker_and_backup_cannot_overwrite_model(tmp_path):
    """ADVERSARY: a marker rewritten consistently (fresh uuid, real model path, manifest of a forged backup)
    must not let rollback replace the model with forged content."""
    import hashlib
    import uuid
    m, _ = _applied(tmp_path)
    original = _snap(m)
    bk = tmp_path / "work" / "backup"
    (bk / "definition" / "tables" / "FactClaims.tmdl").write_text("table FactClaims\n", encoding="utf-8")
    mk = tmp_path / "work" / ".smguard-work"
    j = json.loads(mk.read_text())
    sample = j["manifest"]
    # rebuild the manifest in the same shape the tool uses
    if isinstance(sample, dict):
        j["manifest"] = {k: hashlib.sha256((bk / k).read_bytes()).hexdigest() for k in sample}
    j["signature"] = str(uuid.uuid4())
    mk.write_text(json.dumps(j, indent=1))
    try:
        rollback(tmp_path / "work", m)
    except Exception:
        pass
    assert _snap(m) == original


# ---------- 3. sign-off cannot be a placeholder ----------

@pytest.mark.parametrize("reviewer", ["", "   ", "TBD", "PLACEHOLDER", "n/a", "-", "x", "reviewer", "todo"])
def test_placeholder_reviewer_is_not_a_signoff(tmp_path, reviewer):
    """ADVERSARY: an empty or placeholder reviewer does not count as a human sign-off."""
    from smguard.demo import load_acknowledgements
    f = tmp_path / "acks.json"
    f.write_text(json.dumps([{"measure": "M", "reviewer": reviewer, "date": "2026-10-23", "reason": "checked"}]))
    assert "M" not in load_acknowledgements(f)


def test_invalid_date_is_not_a_signoff(tmp_path):
    """ADVERSARY: a sign-off with a non-date ('someday') is not accepted."""
    from smguard.demo import load_acknowledgements
    f = tmp_path / "acks.json"
    f.write_text(json.dumps([{"measure": "M", "reviewer": "Jane Reviewer", "date": "someday", "reason": "checked"}]))
    assert "M" not in load_acknowledgements(f)


def test_committed_signoff_marked_placeholder_does_not_make_env_safe():
    """ADVERSARY: results/acknowledgements.json literally says PLACEHOLDER; an env must not be 'safe' on it."""
    acks = json.loads((PROJECT / "results" / "acknowledgements.json").read_text(encoding="utf-8"))
    placeholder = {a["measure"] for a in acks if re.search(r"placeholder", a.get("reviewer", ""), re.I)}
    s = json.loads((PROJECT / "results" / "summary.json").read_text())
    for env, e in s["envs"].items():
        if set(e.get("untested_measures", [])) & placeholder:
            assert e["safe"] is False, env
