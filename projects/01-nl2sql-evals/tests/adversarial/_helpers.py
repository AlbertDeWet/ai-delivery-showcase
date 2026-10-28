"""ADVERSARY: shared helpers for the adversary's tests (synthetic records only, no live calls)."""

import json
import subprocess

from nl2sql import evals


def rec(cid, correct, action="sql", error="", in_tok=0, out_tok=0, category="simple", repeat=0):
    return {"id": cid, "category": category, "repeat": repeat, "expect": ["answer"], "question": "q",
            "action": action, "correct": correct, "reason": "", "sql": "", "guard_reasons": [], "error": error,
            "latency_s": 1.0, "input_tokens": in_tok, "output_tokens": out_tok, "cost_usd": None, "retries": 0,
            "first_error": "", "check": {"status": "abstain", "key_number": None, "control": None, "note": "",
                                         "input_tokens": 0, "output_tokens": 0}}


def run(records, repeats=1, tier="haiku", model="claude-haiku-5-5", suite="questions", label=None):
    return {"provider": "claude-cli", "tier": tier, "model": model, "suite": suite, "repeats": repeats,
            "n_cases": len({r["id"] for r in records}), "case_ids": sorted({r["id"] for r in records}),
            "label": label, "timestamp": "2026-10-23T00:00:00+00:00",
            "summary": evals.summarize(records, repeats), "records": records}


class FakeRunner:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.returncode, self.calls = stdout, returncode, 0

    def __call__(self, cmd, **kw):
        self.calls += 1
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def envelope_without_usage(text, is_error=False):
    return json.dumps({"type": "result", "is_error": is_error, "result": text})
