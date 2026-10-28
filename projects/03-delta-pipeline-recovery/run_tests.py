"""Run the builder suite and the adversary suite separately and APPEND both outputs to results/test_runs.txt.

The file is an append-only history: every line in it is a real run. (One adversary test checks the README
counts against this file, so the history is needed for the README claim and the record to agree.)
Usage: python run_tests.py [builder|adversary|all]
"""
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "results" / "test_runs.txt"
SUITES = {"builder": ["tests", "--ignore=tests/adversarial"], "adversary": ["tests/adversarial"]}


def run(label, args):
    try:
        import xdist  # noqa: F401  (parallel workers cut wall time; same tests, same asserts)
        par = ["-n", "4"]
    except ImportError:
        par = []
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *par, *args],
                       cwd=ROOT, capture_output=True, text=True)
    tail = [ln for ln in p.stdout.splitlines() if ln.strip()][-1]
    stamp = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    return f"## {label} ({stamp})\n$ pytest {' '.join(args)}\n{tail}\n", p.returncode


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    OUT.parent.mkdir(exist_ok=True)
    if not OUT.exists():
        OUT.write_text("# Recorded test runs (append-only)\n\n")
    rc = 0
    for label, args in SUITES.items():
        if which in ("all", label):
            txt, code = run(label, args)
            rc |= code
            with OUT.open("a") as f:  # appended after each suite so later suites can read it
                f.write(txt + "\n")
            print(txt)
    sys.exit(rc)
