"""ADVERSARY round 5: skeleton smuggling, ref consistency, sign-off date/reviewer edge cases,
signed-marker key handling, CI compatibility. Fast: minimal TMDL, no demo runs."""
import datetime as dt
import json
import re
from pathlib import Path

import pytest
from helpers import make_model, make_reports, removed_columns
from test_adv_round4 import CULTURE, MODEL, VIS, skeleton

from smguard.trim import apply, plan, rollback

PROJECT = Path(__file__).resolve().parents[2]
REPO = PROJECT.parents[1]
UNSUPPORTED = re.compile(r"unsupported", re.I)


def _refuses(m, r):
    try:
        plan(m, r)
    except Exception as e:  # noqa: BLE001
        return bool(UNSUPPORTED.search(str(e)))
    return False


@pytest.fixture
def keydir(tmp_path, monkeypatch):
    d = tmp_path / "keys"
    monkeypatch.setenv("SMGUARD_KEY_DIR", str(d))
    return d


# ---------- skeleton: still useful ----------

def test_skeleton_with_pbi_editor_settings_accepted(tmp_path):
    """ADVERSARY: a real PBIP SemanticModel folder carries .pbi/editorSettings.json (committed); it must not cause a refusal."""
    m = skeleton(tmp_path, extra={".pbi/editorSettings.json": json.dumps({"version": "1.0", "showHiddenFields": True})})
    plan(m, make_reports(tmp_path, fields=VIS))


def test_skeleton_with_relationships_and_model_properties_accepted(tmp_path):
    """ADVERSARY: common model.tmdl properties (discourageImplicitMeasures, ref table order differing from files) are accepted."""
    mt = MODEL.replace("\tsourceQueryCulture: en-US\n", "\tsourceQueryCulture: en-US\n\tdiscourageImplicitMeasures\n")
    plan(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


# ---------- skeleton: smuggling ----------

def test_table_smuggled_into_model_tmdl_refused(tmp_path):
    """ADVERSARY: a full table (with a calculated partition using a fact column) inline in model.tmdl is refused."""
    mt = MODEL + ("\ntable Smuggled\n\tcolumn S\n\t\tdataType: string\n\t\tsourceColumn: [S]\n\n"
                  "\tpartition Smuggled = calculated\n\t\tsource = VALUES(FactClaims[ClaimStatus])\n")
    assert _refuses(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


def test_expression_smuggled_under_model_object_refused(tmp_path):
    """ADVERSARY: a shared `expression` nested under the `model` object (not at root) is refused."""
    mt = MODEL.replace("\tculture: en-US\n", "\tculture: en-US\n\texpression P = \"x\" meta [IsParameterQuery=true]\n")
    assert _refuses(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


def test_role_smuggled_under_query_group_refused(tmp_path):
    """ADVERSARY: an RLS role indented under a queryGroup in model.tmdl is refused, not read as a group child."""
    mt = MODEL.replace("queryGroup 'Fact Tables'\n",
                       "queryGroup 'Fact Tables'\n\trole Regional\n\t\ttablePermission DimPolicy = DimPolicy[Region] = \"N\"\n")
    assert _refuses(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


def test_measure_smuggled_into_culture_file_refused(tmp_path):
    """ADVERSARY: a culture file that also declares a table/measure is refused."""
    cu = CULTURE + "\ntable FactClaims\n\tmeasure Hidden = SUM(FactClaims[ReserveAmount])\n"
    m = skeleton(tmp_path, extra={"definition/cultures/en-US.tmdl": cu})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_translations_in_culture_refused(tmp_path):
    """ADVERSARY: culture translations (objects referencing columns by name) are refused, not ignored."""
    cu = CULTURE + "\n\ttranslations\n\t\tmodel Model\n\t\t\ttable FactClaims\n\t\t\t\tcolumn ReserveAmount\n\t\t\t\t\tcaption: Réserve\n"
    m = skeleton(tmp_path, extra={"definition/cultures/en-US.tmdl": cu})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_linguistic_metadata_body_cannot_hide_unsupported_object(tmp_path):
    """ADVERSARY: an object placed at cultureInfo child depth AFTER the linguisticMetadata body is still checked."""
    cu = CULTURE + "\tkpi Smuggled\n"
    m = skeleton(tmp_path, extra={"definition/cultures/en-US.tmdl": cu})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_annotation_body_cannot_hide_unsupported_object(tmp_path):
    """ADVERSARY: a multi-line annotation value followed by a root-level `expression` is still refused."""
    mt = MODEL + "\nannotation Big =\n\t\t[\"a\",\n\t\t \"b\"]\n\nexpression Hidden = 1\n"
    assert _refuses(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


# ---------- ref consistency ----------

def test_duplicate_ref_refused(tmp_path):
    """ADVERSARY: the same `ref table` twice is an inconsistent model -> refuse."""
    assert _refuses(skeleton(tmp_path, model_tmdl=MODEL + "ref table FactClaims\n"), make_reports(tmp_path, fields=VIS))


def test_ref_culture_without_file_refused(tmp_path):
    """ADVERSARY: `ref culture en-GB` with no cultures/en-GB.tmdl is refused."""
    assert _refuses(skeleton(tmp_path, model_tmdl=MODEL + "ref culture en-GB\n"), make_reports(tmp_path, fields=VIS))


def test_table_file_without_ref_refused(tmp_path):
    """ADVERSARY: a table file that model.tmdl does not `ref` is inconsistent (model.tmdl lists every table) -> refuse."""
    mt = MODEL.replace("ref table DimDate\n", "")
    assert _refuses(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


def test_ref_self_cycle_refused(tmp_path):
    """ADVERSARY: a `ref model Model` (the model referencing itself) is refused, not ignored."""
    assert _refuses(skeleton(tmp_path, model_tmdl=MODEL + "ref model Model\n"), make_reports(tmp_path, fields=VIS))


def test_ref_case_mismatch_accepted(tmp_path):
    """ADVERSARY: `ref table factclaims` (case differs from the file's table name) still resolves; DAX/TMDL names are case-insensitive."""
    mt = MODEL.replace("ref table FactClaims", "ref table factclaims")
    plan(skeleton(tmp_path, model_tmdl=mt), make_reports(tmp_path, fields=VIS))


# ---------- sign-off: reviewer and date ----------

def _problem(**kw):
    from smguard.demo import signoff_problem
    rec = {"measure": "M", "reviewer": "Jane Reviewer", "date": dt.date.today().isoformat(),
           "reason": "Checked the TMDL; definition unchanged.",
           # Round 6 adjudication: fields required since Round 5 (reviewer_role, definition_sha256)
           "reviewer_role": "human", "definition_sha256": "0" * 64, **kw}
    return signoff_problem(rec)


@pytest.mark.parametrize("reviewer", ["T.B.D.", "ＴＢＤ", "tbd!", "P E N D I N G", "smguard", "Claude", "Claude Code agent"])
def test_disguised_placeholder_or_tool_reviewer_rejected(reviewer):
    """ADVERSARY: disguised placeholders and tool/agent names are not a HUMAN sign-off."""
    assert _problem(reviewer=reviewer) is not None


@pytest.mark.parametrize("date", ["0001-01-01", "1900-01-01"])
def test_absurd_past_date_rejected(date):
    """ADVERSARY: a sign-off dated before the project could exist is not credible."""
    assert _problem(date=date) is not None


def test_today_is_accepted():
    """ADVERSARY: a sign-off dated today (local) is accepted (no off-by-one)."""
    assert _problem() is None


def test_datetime_with_timezone_handled():
    """ADVERSARY: an ISO datetime with offset ('2026-10-01T09:00:00+02:00') is either accepted as that date or
    rejected with a clear 'ISO date' reason - never crashes."""
    p = _problem(date="2026-10-01T09:00:00+02:00")
    assert p is None or "date" in p


def test_tomorrow_in_utc_plus_14_is_not_future_relative_to_reviewer():
    """ADVERSARY: 'future' is judged in UTC-aware terms: a date that is today somewhere on Earth
    (UTC+14) must not be rejected as future."""
    tomorrow_utc14 = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=14)).date()
    assert _problem(date=tomorrow_utc14.isoformat()) is None


def test_signoff_bound_to_definition():
    """ADVERSARY: a sign-off must name the exact definition it reviewed (hash); a record without one cannot
    keep covering the measure after its DAX changes in a later release."""
    from smguard.demo import signoff_problem
    rec = {"measure": "M", "reviewer": "Jane Reviewer", "date": "2026-10-01", "reason": "Checked."}
    assert signoff_problem(rec) is not None


# ---------- signed-marker key handling ----------

def _applied(tmp_path):
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    apply(m, tmp_path, r, tmp_path / "work", plan(m, r))
    return m, r


def _snap(p):
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


def test_key_deleted_mid_flow_rollback_refuses_safely(tmp_path, keydir):
    """ADVERSARY: if the key vanishes between apply and rollback, rollback refuses and the target is untouched."""
    m, _ = _applied(tmp_path)
    (m / "definition" / "tables" / "FactClaims.tmdl").write_text("damaged\n")
    snap = _snap(m)
    (keydir / "marker.key").unlink()
    with pytest.raises(Exception):
        rollback(tmp_path / "work", m)
    assert _snap(m) == snap


def test_key_deleted_mid_flow_explains_cause(tmp_path, keydir):
    """ADVERSARY: when the key is missing/regenerated, the error says so (not 'forged or edited'), so a user
    is not told their own backup was tampered with."""
    m, _ = _applied(tmp_path)
    (keydir / "marker.key").unlink()
    with pytest.raises(Exception) as ei:
        rollback(tmp_path / "work", m)
    assert re.search(r"key", str(ei.value), re.I) and not re.search(r"forged", str(ei.value), re.I)


def test_rollback_does_not_silently_create_new_key(tmp_path, keydir):
    """ADVERSARY: verifying a marker must not silently mint a NEW key (that destroys the evidence and invalidates every other marker)."""
    _applied(tmp_path)
    (keydir / "marker.key").unlink()
    try:
        rollback(tmp_path / "work", tmp_path / "src" / "model")
    except Exception:
        pass
    assert not (keydir / "marker.key").exists()


def test_empty_key_file_refused(tmp_path, keydir):
    """ADVERSARY: an empty (0-byte) key makes every MAC forgeable; apply must refuse or regenerate it."""
    keydir.mkdir(parents=True)
    (keydir / "marker.key").write_bytes(b"")
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    try:
        apply(m, tmp_path, r, tmp_path / "work", plan(m, r))
    except Exception:
        return
    assert len((keydir / "marker.key").read_bytes()) >= 16


def test_key_dir_inside_work_dir_refused(tmp_path, monkeypatch):
    """ADVERSARY: a key stored inside the work folder (readable by whoever can edit the work folder) defeats signing; refuse."""
    monkeypatch.setenv("SMGUARD_KEY_DIR", str(tmp_path / "work" / "keys"))
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    with pytest.raises(Exception):
        apply(m, tmp_path, r, tmp_path / "work", plan(m, r))


def test_key_dir_inside_git_repo_refused(tmp_path, monkeypatch):
    """ADVERSARY: a key directory inside a git repository (could be committed/pushed) is refused."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    monkeypatch.setenv("SMGUARD_KEY_DIR", str(tmp_path / "repo" / "keys"))
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    with pytest.raises(Exception):
        apply(m, tmp_path, r, tmp_path / "work", plan(m, r))


def test_no_key_file_tracked_in_repo():
    """ADVERSARY: no marker.key exists anywhere in the repository tree."""
    assert not [p for p in REPO.rglob("marker.key") if ".venv" not in p.parts]


def test_gitignore_covers_key_and_work():
    """ADVERSARY: .gitignore excludes marker.key / .smguard and the work folder."""
    gi = ""
    for f in (REPO / ".gitignore", PROJECT / ".gitignore"):
        if f.is_file():
            gi += f.read_text(encoding="utf-8")
    assert re.search(r"marker\.key|\.smguard", gi) and re.search(r"(^|/)work/?$", gi, re.M)


# ---------- CI ----------

def test_ci_does_not_treat_demo_exit_3_as_failure():
    """ADVERSARY: if CI runs the demo, exit code 3 ('pending human sign-off') must be tolerated explicitly."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    if "smguard demo" in ci:
        assert re.search(r"\$\?\s*-(eq|ne)\s*3|exit 3|== 3|-eq 3", ci)


def test_ci_python_matches_brief():
    """ADVERSARY: CI runs the Python version the brief mandates (3.13), not 3.12."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "3.12" not in ci or "3.13" in ci


def test_tests_isolate_marker_key_from_home():
    """ADVERSARY: the test suite must not write a real ~/.smguard key (conftest sets SMGUARD_KEY_DIR to a tmp dir)."""
    conf = (PROJECT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "SMGUARD_KEY_DIR" in conf
