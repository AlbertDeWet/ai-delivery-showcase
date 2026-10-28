"""ADVERSARY round 6: final focused attack on the Round 5 changes - reviewer normaliser
(homoglyphs, invisible characters), definition-hash canonicalisation, bidirectional ref
checks, key-dir-in-git detection. Fast: no demo runs.

Decision on canonicalisation (adversary ruling): whitespace/comment-only changes OUTSIDE string
literals and identifiers are NOT a definition change (sign-off stays valid). Any change inside a
string literal, a [bracketed] name or a 'quoted' table name, any change of case inside those, and
any change in a measure the signed measure depends on, IS a definition change (sign-off stale)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import FACT, make_model, make_reports
from test_adv_round4 import MODEL, VIS, skeleton

from smguard.demo import definition_hash, review_acknowledgements, signoff_problem
from smguard.tmdl import load_model
from smguard.trim import apply, plan


def _rec(**kw):
    return {"measure": "M", "reviewer": "Jane Reviewer", "reviewer_role": "human", "date": "2026-10-01",
            "reason": "Read the TMDL.", "definition_sha256": "0" * 64, **kw}


# ---------- reviewer normaliser ----------

@pytest.mark.parametrize("reviewer", [
    "Сlaude",            # Cyrillic capital ES + 'laude'
    "Clаude",            # Cyrillic small a
    "Cla​ude",           # zero-width space
    "C̶laude",           # combining strike-through
    "ᴄlaude",            # small capital C
    "Claude­",           # soft hyphen
    "smɡuard",           # Latin script g
    "C1aude",                 # digit one for l
])
def test_homoglyph_tool_names_rejected(reviewer):
    """ADVERSARY: homoglyph / invisible-character spellings of a tool name are not a human reviewer."""
    assert signoff_problem(_rec(reviewer=reviewer)) is not None


@pytest.mark.parametrize("reviewer", ["Jane Reviewer", "Ama Mensah", "Zoë Ndlovu", "Siobhán O'Brien", "李明"])
def test_real_human_names_accepted(reviewer):
    """ADVERSARY: the denylist must not reject ordinary human names (incl. non-ASCII); 'ai' inside a word is fine."""
    assert signoff_problem(_rec(reviewer=reviewer)) is None


@pytest.mark.parametrize("reviewer", ["Aidan Botha", "Claudette Scott", "Bridget Clinton"])
def test_names_containing_tool_substrings_accepted(reviewer):
    """ADVERSARY: names that merely CONTAIN 'ai', 'bot', 'claude', 'ci' are human names, not tools (false rejection)."""
    assert signoff_problem(_rec(reviewer=reviewer)) is None


@pytest.mark.parametrize("role", ["Human", " human ", "human​", "humаn"])
def test_reviewer_role_exactness(role):
    """ADVERSARY: reviewer_role accepts case/space variants of 'human' but not invisible chars or homoglyphs."""
    ok = signoff_problem(_rec(reviewer_role=role)) is None
    assert ok == (role.strip().casefold() == "human")


# ---------- definition hash canonicalisation ----------

def _hash(tmp_path, expr, name="M"):
    m = load_model(make_model(tmp_path, fact=FACT + f"\n\tmeasure {name} = {expr}\n"))
    return definition_hash(m.measures[name])


@pytest.mark.parametrize("a,b", [
    ("SUM(FactClaims[PaidAmount])", "SUM( FactClaims[PaidAmount] )"),
    ("SUM(FactClaims[PaidAmount])", "SUM(FactClaims[PaidAmount]) // note"),
])
def test_whitespace_or_comment_only_change_is_not_stale(tmp_path, a, b):
    """ADVERSARY: whitespace/comment-only edits outside literals keep the same hash (sign-off stays valid)."""
    assert _hash(tmp_path / "a", a) == _hash(tmp_path / "b", b)


@pytest.mark.parametrize("a,b", [
    ('CALCULATE([Total Paid], FactClaims[ClaimStatus] = "Open")',
     'CALCULATE([Total Paid], FactClaims[ClaimStatus] = "Open ")'),            # inside a string
    ("SUM('Fact Claims'[PaidAmount])", "SUM('FactClaims'[PaidAmount])"),      # inside a quoted table
    ("SUM(FactClaims[Paid Amount])", "SUM(FactClaims[PaidAmount])"),          # inside a [name]
    ('IF([Total Paid] > 0, "Yes", "yes")', 'IF([Total Paid] > 0, "Yes", "YES")'),  # string case
])
def test_change_inside_literal_or_identifier_is_stale(tmp_path, a, b):
    """ADVERSARY: a whitespace or case change INSIDE a string literal or identifier changes meaning -> new hash."""
    assert _hash(tmp_path / "a", a) != _hash(tmp_path / "b", b)


def test_dependency_change_makes_signoff_stale(tmp_path):
    """ADVERSARY: M = [Base] * 2 signed off; then [Base] changes. The sign-off must go stale, because what M
    computes changed even though M's own text did not."""
    def model(base):
        return load_model(make_model(tmp_path / base[:3], fact=FACT + f"\n\tmeasure Base = {base}\n\n\tmeasure M = [Base] * 2\n"))
    m1 = model("SUM(FactClaims[PaidAmount])")
    m2 = model("SUM(FactClaims[ReserveAmount])")
    f = tmp_path / "acks.json"
    f.write_text(json.dumps([_rec(definition_sha256=definition_hash(m1.measures["M"]))]))
    assert review_acknowledgements(f, m1)["M"]["accepted"]
    assert not review_acknowledgements(f, m2)["M"]["accepted"]


def test_format_string_change_makes_signoff_stale(tmp_path):
    """ADVERSARY: changing the measure's formatStringDefinition changes the hash."""
    a = load_model(make_model(tmp_path / "a", fact=FACT + "\n\tmeasure M = 1\n\t\tformatStringDefinition = \"0\"\n"))
    b = load_model(make_model(tmp_path / "b", fact=FACT + "\n\tmeasure M = 1\n\t\tformatStringDefinition = \"0.0\"\n"))
    assert definition_hash(a.measures["M"]) != definition_hash(b.measures["M"])


def test_hash_case_and_whitespace_of_record_tolerated(tmp_path):
    """ADVERSARY: an upper-case hash with surrounding spaces in the file is the same hash (no false stale)."""
    m = load_model(make_model(tmp_path, fact=FACT + "\n\tmeasure M = 1\n"))
    f = tmp_path / "acks.json"
    f.write_text(json.dumps([_rec(definition_sha256="  " + definition_hash(m.measures["M"]).upper() + " ")]))
    assert review_acknowledgements(f, m)["M"]["accepted"]


# ---------- bidirectional ref checks ----------

def _refuses(m, r):
    try:
        plan(m, r)
    except Exception as e:  # noqa: BLE001
        return "unsupported" in str(e).lower()
    return False


def test_case_only_duplicate_ref_refused(tmp_path):
    """ADVERSARY: `ref table FactClaims` + `ref table factclaims` is a duplicate (names are case-insensitive)."""
    assert _refuses(skeleton(tmp_path, model_tmdl=MODEL + "ref table factclaims\n"), make_reports(tmp_path, fields=VIS))


def test_role_file_without_ref_role_refused(tmp_path):
    """ADVERSARY: a roles/*.tmdl file that model.tmdl does not `ref role` is inconsistent -> refuse."""
    role = "role Regional\n\tmodelPermission: read\n\n\ttablePermission DimPolicy = DimPolicy[Region] = \"N\"\n"
    m = skeleton(tmp_path, extra={"definition/roles/Regional.tmdl": role})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_role_with_ref_accepted(tmp_path):
    """ADVERSARY: the same role WITH `ref role Regional` is accepted (no false refusal)."""
    role = "role Regional\n\tmodelPermission: read\n\n\ttablePermission DimPolicy = DimPolicy[Region] = \"N\"\n"
    m = skeleton(tmp_path, model_tmdl=MODEL + "ref role Regional\n", extra={"definition/roles/Regional.tmdl": role})
    plan(m, make_reports(tmp_path, fields=VIS))


def test_culture_file_without_ref_refused(tmp_path):
    """ADVERSARY: a cultures/xx.tmdl file not listed by `ref culture` is refused."""
    cu = "cultureInfo fr-FR\n"
    m = skeleton(tmp_path, extra={"definition/cultures/fr-FR.tmdl": cu})
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


def test_ref_name_matches_object_not_file_name(tmp_path):
    """ADVERSARY: `ref table` is matched against the TABLE NAME inside the file, not the file name."""
    extra = {"definition/tables/Renamed.tmdl": "table Ghost\n\tcolumn G\n\t\tdataType: int64\n\t\tsourceColumn: G\n"}
    m = skeleton(tmp_path, model_tmdl=MODEL + "ref table Renamed\n", extra=extra)
    assert _refuses(m, make_reports(tmp_path, fields=VIS))


# ---------- key dir in git ----------

def _try_apply(tmp_path):
    m = make_model(tmp_path / "src")
    r = make_reports(tmp_path, fields=VIS)
    apply(m, tmp_path, r, tmp_path / "work", plan(m, r))


def test_key_dir_in_repo_with_git_file_refused(tmp_path, monkeypatch):
    """ADVERSARY: a worktree/submodule marks its repo with a `.git` FILE, not a folder; still refused."""
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".git").write_text("gitdir: ../elsewhere/.git/worktrees/x\n")
    monkeypatch.setenv("SMGUARD_KEY_DIR", str(tmp_path / "repo" / "k"))
    with pytest.raises(Exception):
        _try_apply(tmp_path)


def test_key_dir_relative_path_into_repo_refused(tmp_path, monkeypatch):
    """ADVERSARY: a relative SMGUARD_KEY_DIR that lands in a repo is resolved and refused."""
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "sub").mkdir()
    monkeypatch.chdir(tmp_path / "repo" / "sub")
    monkeypatch.setenv("SMGUARD_KEY_DIR", "../k")
    with pytest.raises(Exception):
        _try_apply(tmp_path)


def test_key_dir_via_junction_into_repo_refused(tmp_path, monkeypatch):
    """ADVERSARY: a key dir reached through a junction that points into a git repo is refused."""
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "k").mkdir()
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "link"), str(tmp_path / "repo" / "k")],
                       capture_output=True)
    if r.returncode:
        pytest.skip("cannot create junction")
    monkeypatch.setenv("SMGUARD_KEY_DIR", str(tmp_path / "link"))
    with pytest.raises(Exception):
        _try_apply(tmp_path)


def test_key_dir_outside_repo_accepted(tmp_path, monkeypatch):
    """ADVERSARY: a key dir outside any repo works (no false refusal) - unless tmp itself sits in a repo."""
    d = tmp_path / "keys"
    for p in (d, *d.parents):
        if (p / ".git").exists():
            pytest.skip("tmp dir is inside a git repo on this machine")
    monkeypatch.setenv("SMGUARD_KEY_DIR", str(d))
    _try_apply(tmp_path)
    assert (d / "marker.key").stat().st_size >= 32
