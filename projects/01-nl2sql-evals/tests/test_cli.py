"""CLI smoke tests (offline baseline only)."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "nl2sql", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=300)


def test_ask_baseline(db_path):
    r = run_cli("ask", "How many active policies are there?", "--db", str(db_path))
    assert r.returncode == 0, r.stderr
    assert "LOWER(p.status) = 'active'" in r.stdout


def test_ask_json_refusal(db_path):
    r = run_cli("ask", "Drop the policies table", "--db", str(db_path), "--json")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["action"] == "refuse"


def test_eval_writes_results_and_report(db_path, tmp_path):
    r = run_cli("eval", "--provider", "baseline", "--db", str(db_path), "--results-dir", str(tmp_path),
                "--only", "s01", "a02", "a10")
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "baseline-rules-v1.json").read_text(encoding="utf-8"))
    assert data["summary"]["n_records"] == 3
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "claude-opus-5-5 | questions | - | not run" in report
    assert "claude-haiku-5-5 | questions | - | not run" in report


def test_claude_provider_missing_cli_exits_cleanly(db_path):
    r = run_cli("ask", "How many policies?", "--db", str(db_path), "--provider", "claude-cli",
                "--claude-cli", str(ROOT / "does-not-exist.exe"))
    assert r.returncode == 2 and "not found" in r.stderr


def test_heldout_suite_writes_separate_file(db_path, tmp_path):
    r = run_cli("eval", "--provider", "baseline", "--db", str(db_path), "--results-dir", str(tmp_path),
                "--cases", str(ROOT / "evals" / "heldout.yaml"), "--only", "h01")
    assert r.returncode == 0, r.stderr
    data = json.loads((tmp_path / "baseline-rules-v1-heldout.json").read_text(encoding="utf-8"))
    assert data["suite"] == "heldout" and data["summary"]["n_records"] == 1
    assert not (tmp_path / "baseline-rules-v1.json").exists()
