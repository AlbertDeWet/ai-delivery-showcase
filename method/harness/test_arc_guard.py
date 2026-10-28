#!/usr/bin/env python3
"""Tests for arc_guard.py.

A hook that is wrong fails silently, so every path is exercised against the real stdin
contract rather than eyeballed. The case that matters most is #4: the gate must return
exit 2, because exit 1 does not block.

    python test_arc_guard.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arc_guard.py")
PY = sys.executable or "python"

passed, failed = 0, []


def run(event, payload, cwd, env=None):
    e = dict(os.environ)
    e["CLAUDE_PROJECT_DIR"] = cwd
    if env:
        e.update(env)
    p = subprocess.run([PY, GUARD, event], input=json.dumps(payload),
                       capture_output=True, text=True, cwd=cwd, env=e)
    return p.returncode, p.stdout, p.stderr


def check(name, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print("  PASS  " + name)
    else:
        failed.append(name)
        print("  FAIL  " + name + ("  -- " + detail if detail else ""))


def mkproject(with_marker=True, cfg=None):
    d = tempfile.mkdtemp(prefix="arctest-")
    if with_marker:
        cfg = cfg if cfg is not None else {
            "checks": {"test": "pytest -q", "lint": "ruff check"},
            "test_paths": ["tests/**", "**/*.test.*", "**/test_*.py"],
        }
        with open(os.path.join(d, ".arc"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    return d


def sd(root):
    return os.path.join(root, ".claude", "arc")


def turnlog(root):
    p = os.path.join(sd(root), "turn.log")
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


print("arc_guard.py\n")

# ---------------------------------------------------------------- 1. opt-in gate
print("1. Opt-in gate -- no .arc means no behaviour at all")
d = mkproject(with_marker=False)
for ev, payload in [("stop", {}), ("sessionstart", {}),
                    ("pretooluse", {"tool_name": "Edit", "tool_input": {"file_path": "tests/x.py"}}),
                    ("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}),
                    ("userpromptsubmit", {})]:
    rc, out, err = run(ev, payload, d)
    check("%-16s exits 0 and is silent" % ev, rc == 0 and not out.strip(), "rc=%s out=%r" % (rc, out[:60]))
check("no state directory created", not os.path.exists(sd(d)))
shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- 2. observation
print("\n2. PostToolUse observation")
d = mkproject()
run("userpromptsubmit", {}, d)
run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q tests/"}}, d)
run("posttooluse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d, "src", "a.py")}}, d)
log = turnlog(d)
check("bash command recorded", "BASH\tpytest -q tests/" in log, repr(log))
check("edit recorded, path relative", "EDIT\tsrc/a.py" in log, repr(log))
rc, out, err = run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "x"}}, d)
check("observation never blocks", rc == 0)

# ---------------------------------------------------------------- 3. read-only turn passes
print("\n3. Stop gate -- read-only turn")
run("userpromptsubmit", {}, d)
run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}, d)
rc, out, err = run("stop", {}, d)
check("no edits -> turn allowed to end", rc == 0, "rc=%s" % rc)

# ---------------------------------------------------------------- 4. THE GATE
print("\n4. Stop gate -- file-modifying turn  (the one that matters)")
run("userpromptsubmit", {}, d)
run("posttooluse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d, "src", "a.py")}}, d)
rc, out, err = run("stop", {}, d)
check("edits + no checks -> EXIT 2 (blocks)", rc == 2, "rc=%s  <-- 1 would NOT block" % rc)
check("reason names the missing commands", "pytest -q" in err and "ruff check" in err, repr(err[:160]))
check("reason names the changed file", "src/a.py" in err, repr(err[:160]))

run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, d)
rc, out, err = run("stop", {}, d)
check("one check run, one still missing -> still blocks", rc == 2, "rc=%s" % rc)
check("only the missing check is named", "ruff check" in err and "pytest" not in err.split("Run these")[-1],
      repr(err[-120:]))

run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "ruff check ."}}, d)
rc, out, err = run("stop", {}, d)
check("all checks run -> turn allowed to end", rc == 0, "rc=%s err=%r" % (rc, err[:80]))

print("\n   arguments must not defeat the match")
run("userpromptsubmit", {}, d)
run("posttooluse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d, "b.py")}}, d)
run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "pytest -q tests/unit --tb=short"}}, d)
run("posttooluse", {"tool_name": "Bash", "tool_input": {"command": "ruff check src/ --fix"}}, d)
rc, out, err = run("stop", {}, d)
check("checks matched despite extra args", rc == 0, "rc=%s err=%r" % (rc, err[:80]))

print("\n   turn boundary resets the judgement")
run("userpromptsubmit", {}, d)
run("posttooluse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d, "c.py")}}, d)
rc, out, err = run("stop", {}, d)
check("new turn does not inherit last turn's checks", rc == 2, "rc=%s" % rc)

# ---------------------------------------------------------------- 5. test-file lock
print("\n5. PreToolUse -- test-file lock")
edit_test = {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d, "tests", "test_a.py")}}
edit_src = {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d, "src", "a.py")}}

rc, out, err = run("pretooluse", edit_test, d)
check("no fix-lock -> test edit allowed", rc == 0 and not out.strip(), "out=%r" % out[:80])

open(os.path.join(sd(d), "fix.lock"), "w").close()
rc, out, err = run("pretooluse", edit_test, d)
denied = False
try:
    denied = json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
except Exception:
    pass
check("fix-lock -> test edit DENIED", denied, "out=%r" % out[:120])
check("deny carries a readable reason",
      "weaken" in out or "cannot be edited" in out, "out=%r" % out[:120])
check("deny is returned with exit 0 (JSON decision)", rc == 0, "rc=%s" % rc)

rc, out, err = run("pretooluse", edit_src, d)
check("fix-lock -> source edit still allowed", not out.strip(), "out=%r" % out[:80])

rc, out, err = run("pretooluse", {"tool_name": "Read", "tool_input": {"file_path": "tests/test_a.py"}}, d)
check("non-edit tool unaffected", not out.strip())
os.remove(os.path.join(sd(d), "fix.lock"))

# ---------------------------------------------------------------- 6. cost ledger
print("\n6. PostToolUse -- cost ledger")
resp = ("### Status\nCOMPLETE\n\n### Runtime check\nModel: claude-sonnet-5\n\n"
        "### Rung verdict\ntoo high -- a cheaper rung would have done it\n")
run("posttooluse", {"tool_name": "Task",
                    "tool_input": {"subagent_type": "arc-r3", "description": "reconcile reports"},
                    "tool_response": resp,
                    "usage": {"subagent_tokens": 14196}}, d)
rows = [json.loads(l) for l in
        open(os.path.join(sd(d), "ledger.jsonl"), encoding="utf-8").read().splitlines() if l.strip()]
check("ledger row written", len(rows) == 1, str(len(rows)))
if rows:
    r = rows[0]
    check("rung captured", r["rung"] == "R3", str(r.get("rung")))
    check("tokens captured", r["tokens"] == 14196, str(r.get("tokens")))
    check("model captured", r["model_reported"] == "claude-sonnet-5", str(r.get("model_reported")))
    check("verdict captured", r["verdict"] == "too high", str(r.get("verdict")))

run("posttooluse", {"tool_name": "Task", "tool_input": {"subagent_type": "Explore"},
                    "tool_response": "x"}, d)
rows2 = open(os.path.join(sd(d), "ledger.jsonl"), encoding="utf-8").read().splitlines()
check("non-ARC agents are not costed", len([l for l in rows2 if l.strip()]) == 1, str(len(rows2)))

# ---------------------------------------------------------------- 7. session start
print("\n7. SessionStart digest")
rc, out, err = run("sessionstart", {}, d)
check("digest printed", rc == 0 and "ARC harness active" in out, "rc=%s" % rc)
check("names the rung ladder", "arc-r2" in out and "sonnet/medium" in out)
check("names the verifier hinge", "MACHINE can tell pass from" in out)  # wraps in the digest
check("project checks surfaced", "pytest -q" in out)
check("digest is lean (<3000 chars)", len(out) < 3000, "%d chars" % len(out))

append_err = os.path.join(sd(d), "hook-errors.log")
with open(append_err, "w", encoding="utf-8") as f:
    f.write("2026-01-01 00:00:00  stop handler: boom\n")
rc, out, err = run("sessionstart", {}, d)
check("a broken hook is surfaced, not silent", "may not be enforcing" in out, out[-120:])
os.remove(append_err)

# ---------------------------------------------------------------- 8. robustness
print("\n8. Robustness -- must never wedge a session")
rc, out, err = run("stop", {}, d, env={"ARC_NONE": "1"})
check("malformed/empty stdin is survivable", rc in (0, 2), "rc=%s" % rc)
p = subprocess.run([PY, GUARD, "stop"], input="not json at all",
                   capture_output=True, text=True, cwd=d,
                   env=dict(os.environ, CLAUDE_PROJECT_DIR=d))
check("garbage stdin does not crash", p.returncode in (0, 2), "rc=%s" % p.returncode)
rc, out, err = run("nonsense-event", {}, d)
check("unknown event is a no-op", rc == 0 and not out.strip())

d2 = mkproject(cfg={})
rc, out, err = run("stop", {}, d2)
check("empty .arc config -> gate disabled, not crashed", rc == 0, "rc=%s" % rc)
shutil.rmtree(d2, ignore_errors=True)

d3 = mkproject(cfg={"checks": {"test": "pytest"}})
run("userpromptsubmit", {}, d3)
run("posttooluse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(d3, "z.py")}}, d3)
rc, out, err = run("stop", {}, d3)
check("no test_paths declared -> gate still works", rc == 2, "rc=%s" % rc)
shutil.rmtree(d3, ignore_errors=True)

shutil.rmtree(d, ignore_errors=True)

# ---------------------------------------------------------------- result
print("\n" + "=" * 60)
print("%d passed, %d failed" % (passed, len(failed)))
for f in failed:
    print("  FAILED: " + f)
sys.exit(1 if failed else 0)
