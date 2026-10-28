"""ADVERSARY round 1: backup/rollback exactness, drift classification, README vs results/."""
import json
import re
import shutil
from pathlib import Path

import pytest
from helpers import DIMDATE, FACT, make_model, make_reports

from smguard.analyze import drift
from smguard.trim import _same_tree, apply, plan, rollback

PROJECT = Path(__file__).resolve().parents[2]


def _snapshot(p: Path) -> dict:
    return {str(f.relative_to(p)): f.read_bytes() for f in p.rglob("*") if f.is_file()}


# ---------- backup / rollback ----------

def test_apply_with_workdir_equal_to_model_does_not_destroy_original(tmp_path, generated):
    """ADVERSARY: apply() must refuse a work_dir that is (or contains) the model folder - it rmtree()s it first."""
    model = tmp_path / "m"
    shutil.copytree(generated / "models" / "DEV", model)
    snap = _snapshot(model)
    p = plan(model, generated / "reports")
    try:
        apply(model, generated / "data" / "DEV" / "v1", generated / "reports", tmp_path, p)
    except Exception:
        pass
    assert model.exists() and _snapshot(model) == snap


def test_rollback_without_backup_does_not_delete_target(tmp_path):
    """ADVERSARY: rollback() with a missing backup must not delete the target before failing."""
    m = make_model(tmp_path)
    snap = _snapshot(m)
    try:
        rollback(tmp_path / "no_such_work", m)
    except Exception:
        pass
    assert m.exists() and _snapshot(m) == snap


def test_rollback_is_byte_exact(tmp_path, generated):
    """ADVERSARY: after apply + damage + rollback the model is byte-identical to the original."""
    model = tmp_path / "m"
    shutil.copytree(generated / "models" / "UAT", model)
    snap = _snapshot(model)
    p = plan(model, generated / "reports")
    apply(model, generated / "data" / "UAT" / "v1", generated / "reports", tmp_path / "work", p)
    (model / "definition" / "tables" / "FactClaims.tmdl").write_text("garbage", encoding="utf-8")
    (model / "stray.txt").write_text("x")
    rollback(tmp_path / "work", model)
    assert _snapshot(model) == snap


def test_trim_does_not_rewrite_untouched_files(tmp_path):
    """ADVERSARY: files with nothing removed stay byte-identical (CRLF / no trailing newline preserved)."""
    m = make_model(tmp_path, dimdate=DIMDATE.replace("\n", "\r\n").rstrip("\r\n"))
    r = make_reports(tmp_path, fields=(("Measure", "FactClaims", "Total Paid"), ("Column", "DimDate", "Year"),
                                       ("Column", "DimDate", "Month"), ("Column", "DimDate", "MonthName")))
    p = plan(m, r)
    assert not [c for c in p["remove_columns"] if c["table"] == "DimDate"]
    out = apply(m, tmp_path, r, tmp_path / "work", p)
    a = (m / "definition" / "tables" / "DimDate.tmdl").read_bytes()
    b = (Path(out["trimmed"]) / "definition" / "tables" / "DimDate.tmdl").read_bytes()
    assert a == b


def test_backup_check_is_not_tautological(tmp_path, generated):
    """ADVERSARY: backup_matches_original compares backup to the original AFTER apply; a backup of the wrong
    folder must fail. Here we prove the check would catch a corrupted backup by corrupting it and re-checking."""
    model = tmp_path / "m"
    shutil.copytree(generated / "models" / "DEV", model)
    p = plan(model, generated / "reports")
    out = apply(model, generated / "data" / "DEV" / "v1", generated / "reports", tmp_path / "work", p)
    assert out["checks"]["backup_matches_original"]
    (Path(out["backup"]) / "definition" / "tables" / "DimDate.tmdl").write_text("x")
    assert not _same_tree(model, Path(out["backup"]))


# ---------- drift ----------

def _two(tmp_path, fact_a=FACT, fact_b=FACT, rels_b=None, dd_b=DIMDATE):
    a = make_model(tmp_path / "A", fact=fact_a)
    kw = {"fact": fact_b, "dimdate": dd_b}
    if rels_b is not None:
        kw["rels"] = rels_b
    b = make_model(tmp_path / "B", **kw)
    return drift({"DEV": a, "PROD": b})


def test_drift_ignores_whitespace_only_differences(tmp_path):
    """ADVERSARY: 'SUM( FactClaims[PaidAmount] )' vs 'SUM(FactClaims[PaidAmount])' is not drift (false positive)."""
    d = _two(tmp_path, fact_b=FACT.replace("SUM(FactClaims[PaidAmount])", "SUM( FactClaims[PaidAmount] )"))
    assert d["drift_count"] == 0, d


def test_drift_detects_datatype_change(tmp_path):
    """ADVERSARY: a column whose dataType differs between envs (int64 vs string) is drift."""
    d = _two(tmp_path, dd_b=DIMDATE.replace("dataType: int64\n\t\tsourceColumn: Year", "dataType: string\n\t\tsourceColumn: Year"))
    assert d["drift_count"] > 0, d


def test_drift_detects_relationship_difference(tmp_path):
    """ADVERSARY: a relationship present in DEV but missing in PROD is drift."""
    d = _two(tmp_path, rels_b="relationship rel1\n\tfromColumn: FactClaims.PolicyKey\n\ttoColumn: DimPolicy.PolicyKey\n")
    assert d["drift_count"] > 0, d


def test_drift_detects_measure_moved_table(tmp_path):
    """ADVERSARY: the same measure homed in a different table is drift (field wells bind by entity)."""
    fact_b = FACT.replace("\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n\n", "")
    b_pol = "table DimPolicy\n\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])\n\n\tcolumn PolicyKey\n\t\tdataType: int64\n"
    a = make_model(tmp_path / "A")
    b = make_model(tmp_path / "B", fact=fact_b, dimpolicy=b_pol)
    assert drift({"DEV": a, "PROD": b})["drift_count"] > 0


# ---------- README vs results/ ----------

def _readme_rows():
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    rows = {}
    for line in text.splitlines():
        m = re.match(r"\| (DEV|UAT|PROD) \| ([\d.]+) \| ([\d.]+) \| (\d+) -> (\d+) \| (\d+) -> (\d+) \| (\d+) -> (\d+) \| (\d+) -> (\d+) \| (\d+)/(\d+) \| (\d+) \| (\d+) \|", line)
        if m:
            rows[m.group(1)] = m.groups()[1:]
    return text, rows


def test_readme_table_matches_results_summary():
    """ADVERSARY: every number in the README results table equals results/summary.json."""
    _, rows = _readme_rows()
    s = json.loads((PROJECT / "results" / "summary.json").read_text())["envs"]
    assert set(rows) == {"DEV", "UAT", "PROD"}
    for env, g in rows.items():
        e = s[env]
        want = (e["est_mb_before"], e["est_mb_after"], e["fact_columns_before"], e["fact_columns_after"],
                e["columns_before"], e["columns_after"], e["measures_before"], e["measures_after"],
                e["broken_before"], e["broken_after"], *e["consumers_after"].split("/"),
                e["regression_checks"], e["unexpected"])
        assert [float(x) for x in g] == [float(x) for x in want], env


def test_readme_prose_numbers_match_results():
    """ADVERSARY: README prose (classification counts, drift 5 -> 1, 17 builder tests) matches results/ and tests/."""
    text, _ = _readme_rows()
    s = json.loads((PROJECT / "results" / "summary.json").read_text())
    p = s["envs"]["PROD"]["regression"]
    assert f"{p.get('IDENTICAL')} identical" in text
    assert f"{p.get('EXPECTED: fresh data')} expected as fresh data" in text
    assert f"from {s['drift_before']} to {s['drift_after']}" in text
    n = sum(len(re.findall(r"^def test_", f.read_text(), re.M)) for f in (PROJECT / "tests").glob("test_*.py"))
    assert f"{n} tests in `tests/`" in text


def test_results_are_reproducible(demo_summary):
    """ADVERSARY: a fresh demo run reproduces results/summary.json exactly (numbers are not stale)."""
    _, fresh = demo_summary
    committed = json.loads((PROJECT / "results" / "summary.json").read_text())
    assert json.loads(json.dumps(fresh)) == committed


def test_readme_claims_no_adversarial_round_is_stale():
    """ADVERSARY: once Round 1 is logged, README must not still claim no adversarial round has run."""
    text = (PROJECT / "README.md").read_text(encoding="utf-8")
    log = (PROJECT / "ADVERSARIAL-LOG.md").read_text(encoding="utf-8")
    if "## Round 1" in log:
        assert "No adversarial round has run yet" not in text
