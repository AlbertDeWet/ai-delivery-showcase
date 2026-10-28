"""ADVERSARY: README numbers checked against results/ (honest-numbers rule)."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def _load(name):
    return json.loads((ROOT / "results" / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,overall,exec_,passk", [
    ("baseline-rules-v1.json", 75.9, 71.4, 75.9),
    ("baseline-rules-v1-heldout.json", 46.7, 36.4, 46.7),
    ("claude-cli-haiku.json", 98.1, 97.6, 98.1),
    ("claude-cli-haiku-heldout.json", 93.3, 97.0, 86.7),
    ("claude-cli-sonnet.json", 96.9, 96.0, 94.4),
    ("claude-cli-sonnet-heldout.json", 93.3, 100.0, 93.3),
    ("derived/claude-cli-haiku-j03-fix.json", 100.0, 100.0, 100.0),
    ("derived/claude-cli-sonnet-j03-fix.json", 98.8, 98.4, 96.3),
    ("derived/claude-cli-sonnet-infra-retry.json", 98.1, 97.6, 98.1),
    ("derived/claude-cli-sonnet-j03-fix+infra-retry.json", 100.0, 100.0, 100.0),
])
def test_readme_table_numbers_match_result_files(name, overall, exec_, passk):
    """ADVERSARY: positive control - the README tables match the recorded and derived files."""
    s = _load(name)["summary"]
    pk = next(v for k, v in s.items() if k.startswith("pass^"))
    assert (s["overall_accuracy_pct"], s["execution_accuracy_pct"], pk) == (overall, exec_, passk)


def test_baseline_latency_in_findings_matches_recorded_run():
    """ADVERSARY: Finding 3 quotes '0.14 s for the offline baseline'; the recorded p50 is 0.123."""
    p50 = _load("baseline-rules-v1.json")["summary"]["latency_p50_s"]
    m = re.search(r"against ([0-9.]+) s for the offline baseline", README)
    assert m and float(m.group(1)) == round(p50, 2)


def test_pre_fix_baseline_numbers_are_backed_by_a_results_file():
    """ADVERSARY: README quotes the pre-j03-fix baseline at 77.8% / 73.8%, but that run was overwritten;
    no file in results/ records it (brief rule 4: every README number comes from results/)."""
    assert "77.8" in README
    text = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "results").rglob("*.json"))
    assert re.search(r'"overall_accuracy_pct": 77\.8', text)


def test_readme_states_pre_rule_runs_have_no_retry_rule_key():
    """ADVERSARY (revised in Round 2 per orchestrator ruling: recorded files are never edited): the
    pre-rule files have no retry_rule key, and the README must say so honestly, not claim 'none'."""
    for name in ("claude-cli-haiku.json", "claude-cli-sonnet.json"):
        assert "retry_rule" not in _load(name)
    flat = " ".join(README.split())
    assert re.search(r"no `retry_rule` key", flat)
    assert not re.search(r"say `retry_rule: none`", flat)


def test_sonnet_rerun_latency_claim():
    """ADVERSARY: positive control - 'all 6 fresh calls finished in 23 to 28 s'."""
    lat = [r["latency_s"] for r in _load("reruns/claude-cli-sonnet-infra-retry.json")["records"]]
    assert len(lat) == 6 and 22.5 <= min(lat) and max(lat) < 28.5
