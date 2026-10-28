#!/usr/bin/env python3
"""
ARC enforcement hooks - one script, dispatched by the event name passed as argv[1].

Turns ARC's process rules from instructions Claude usually follows into code that runs.

    "CLAUDE.md and skills are both instructions that Claude follows, while a hook is code that
     actually runs. If skipping the rule isn't acceptable, don't leave it up to
     instruction-following."
        -- Claude Code training material ("Claude Code in Action")

OPT-IN: every handler exits 0 immediately unless an `.arc` file exists in the project root
(or any parent). No marker, no behaviour change.

EXIT CODES -- the thing that silently defeats hooks:
    0  success. stdout is injected into context on SessionStart / UserPromptSubmit only.
    2  BLOCK. stderr is fed back to Claude so it self-corrects.
    1  does NOT block. Claude runs the command anyway. Never used here, deliberately.

FAILURE POLICY: a crash fails OPEN (exit 0), never closed -- a bug in this file must not be
able to wedge a session. But it is recorded to .claude/arc/hook-errors.log and surfaced at the
next SessionStart, so a silently-broken gate becomes visible rather than staying silent.

Usage (from hooks.json):
    python arc_guard.py pretooluse|posttooluse|stop|sessionstart|userpromptsubmit
"""

import json
import os
import re
import sys
import time
import fnmatch

STATE_DIRNAME = os.path.join(".claude", "arc")
MARKER = ".arc"


# --------------------------------------------------------------------------- plumbing

def read_input():
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def pick(d, *names, default=None):
    """Tolerate snake_case / camelCase differences in the hook payload."""
    for n in names:
        if isinstance(d, dict) and n in d and d[n] is not None:
            return d[n]
    return default


def project_root():
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    cur = os.path.abspath(start)
    while True:
        if os.path.exists(os.path.join(cur, MARKER)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def load_config(root):
    path = os.path.join(root, MARKER)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        return json.loads(text) if text else {}
    except Exception:
        return {}


def state_dir(root):
    d = os.path.join(root, STATE_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def append(path, line):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def record_error(root, msg):
    try:
        if root:
            append(os.path.join(state_dir(root), "hook-errors.log"),
                   "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# --------------------------------------------------------------------------- handlers

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def h_pretooluse(data, root, cfg):
    """Test-file lock: while a fix is in progress, the run may not edit the tests that gate it.

    "an agent fixing code must not be able to weaken the check on that code. A hook that
     blocks edits to test files during a fix task does this."   -- 26-...md, Lesson 8
    """
    tool = pick(data, "tool_name", "toolName", default="")
    if tool not in EDIT_TOOLS:
        return 0

    if not os.path.exists(os.path.join(state_dir(root), "fix.lock")):
        return 0

    ti = pick(data, "tool_input", "toolInput", default={}) or {}
    target = pick(ti, "file_path", "filePath", "path", "notebook_path", default="")
    if not target:
        return 0

    patterns = cfg.get("test_paths") or []
    if not patterns:
        return 0

    rel = target
    try:
        rel = os.path.relpath(os.path.abspath(target), root).replace("\\", "/")
    except Exception:
        pass
    base = os.path.basename(rel)

    for pat in patterns:
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(base, pat):
            reason = (
                "ARC test-file lock: a fix is in progress, so the tests that gate it cannot be "
                "edited. You were about to modify %s, which matches the protected pattern '%s'.\n"
                "Fix the code so the existing test passes. If the test itself is genuinely "
                "wrong, stop and say so rather than changing it."
                % (rel, pat)
            )
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
            sys.stdout.write(json.dumps(out))
            return 0                       # JSON decision is returned with exit 0
    return 0


def h_posttooluse(data, root, cfg):
    """Observation only. Never blocks. Feeds the Stop gate and the cost ledger."""
    tool = pick(data, "tool_name", "toolName", default="")
    sd = state_dir(root)

    if tool == "Bash":
        ti = pick(data, "tool_input", "toolInput", default={}) or {}
        cmd = str(pick(ti, "command", default="")).replace("\n", " ")[:500]
        if cmd:
            append(os.path.join(sd, "turn.log"), "BASH\t" + cmd)

    elif tool in EDIT_TOOLS:
        ti = pick(data, "tool_input", "toolInput", default={}) or {}
        target = pick(ti, "file_path", "filePath", "path", "notebook_path", default="")
        if target:
            try:
                target = os.path.relpath(os.path.abspath(target), root).replace("\\", "/")
            except Exception:
                pass
            append(os.path.join(sd, "turn.log"), "EDIT\t" + str(target))

    elif tool == "Task":
        _ledger(data, root, sd)

    return 0


VERDICT_RE = re.compile(
    r"###\s*Rung verdict\s*\n+\s*[-*]?\s*\**\s*(held|strained|too low|too high|needed it|"
    r"brief was the problem)", re.I)
MODEL_RE = re.compile(r"claude-(haiku|sonnet|opus|fable)[\w.-]*", re.I)


def _ledger(data, root, sd):
    """One row per rung-agent delegation. This is what makes value-for-money measurable
    rather than merely argued: rung + cost + the agent's own verdict on whether the rung
    was right."""
    ti = pick(data, "tool_input", "toolInput", default={}) or {}
    agent = str(pick(ti, "subagent_type", "subagentType", default="") or "")
    if not agent.startswith("arc-"):
        return                                   # only ARC rungs are costed

    resp = pick(data, "tool_response", "toolResponse", "result", default="")
    text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)

    usage = pick(data, "usage", default={}) or {}
    if not isinstance(usage, dict):
        usage = {}
    tokens = pick(usage, "subagent_tokens", "total_tokens", "output_tokens", default=None)
    if tokens is None:
        m = re.search(r"subagent_tokens:\s*(\d+)", text)
        tokens = int(m.group(1)) if m else None

    vm = VERDICT_RE.search(text or "")
    mm = MODEL_RE.search(text or "")

    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "agent": agent,
        "rung": agent.replace("arc-", "").upper() if re.match(r"arc-r\d$", agent) else None,
        "tokens": tokens,
        "model_reported": mm.group(0) if mm else None,
        "verdict": vm.group(1).lower() if vm else None,
        "description": str(pick(ti, "description", default=""))[:120],
    }
    append(os.path.join(sd, "ledger.jsonl"), json.dumps(row, ensure_ascii=False))


def h_userpromptsubmit(data, root, cfg):
    """Turn boundary. Clears the per-turn log so the Stop gate judges THIS turn only.

    Deliberately prints nothing: stdout here would be injected into context on every single
    turn, which buys no extra guarantee and works against keeping context lean.
    """
    try:
        open(os.path.join(state_dir(root), "turn.log"), "w", encoding="utf-8").close()
    except Exception:
        pass
    return 0


def h_stop(data, root, cfg):
    """The verification gate. Refuses to end a turn that changed files without running the
    project's checks.

    "The real gate on an unsupervised run is whether the tests passed, and whether Claude
     actually ran them or only claimed that it did. Don't leave that to trust. Wire it as a
     hook so Claude can't skip it."        -- 15-claude-code-in-action.md, Lesson 8
    """
    checks = cfg.get("checks") or {}
    if not checks:
        return 0                                  # nothing declared, nothing to enforce

    turn_log = os.path.join(state_dir(root), "turn.log")
    if not os.path.exists(turn_log):
        return 0

    with open(turn_log, encoding="utf-8") as f:
        lines = [l.rstrip("\n") for l in f if l.strip()]

    edits = [l.split("\t", 1)[1] for l in lines if l.startswith("EDIT\t")]
    if not edits:
        return 0                                  # read-only turn: pass untouched

    bash = [l.split("\t", 1)[1] for l in lines if l.startswith("BASH\t")]
    blob = "\n".join(bash)

    missing = []
    for name, cmd in checks.items():
        token = str(cmd).strip()
        if not token:
            continue
        # match on the distinctive head of the command, so arguments don't defeat it
        head = " ".join(token.split()[:2])
        if head and head not in blob:
            missing.append((name, token))

    if not missing:
        return 0

    files = ", ".join(sorted(set(edits))[:6])
    more = "" if len(set(edits)) <= 6 else " (+%d more)" % (len(set(edits)) - 6)
    msg = [
        "ARC verification gate: this turn changed files but did not run the project's checks.",
        "",
        "Changed: %s%s" % (files, more),
        "",
        "Run these and paste the real output before finishing:",
    ]
    for name, cmd in missing:
        msg.append("  %-8s %s" % (name + ":", cmd))
    msg += [
        "",
        "Done is the gates being run and observed, with the results stated explicitly -- not",
        "the code looking right. If a check fails, fix the code, not the check.",
    ]
    sys.stderr.write("\n".join(msg) + "\n")
    return 2                                      # 2 blocks. 1 would not.


DIGEST = """\
[ARC harness active in this project]

Routing ladder -- delegate execution to the rung agent, which carries its own model and effort:
  R0 arc-r0 haiku/low     R1 arc-r1 haiku/medium   R2 arc-r2 sonnet/medium  (default)
  R3 arc-r3 sonnet/high   R4 arc-r4 opus/medium    R5 arc-r5 opus/high
  R6 arc-r6 fable/medium  (ceiling; escalation-only, never scored into)
  arc-check  sonnet/high, read-only cold reviewer

Score from base R2: decomposable -1 | machine verifier -1 | classify/extract/reformat -2 |
lookup -2 | debugging +1 | comparing options or architecture +2 | long dependent reasoning +1 |
many files +1 | high stakes +1 (and cheap-iterate OFF). Clamp R0-R5.
Settle the rung first, THEN apply effort signals. +1 effort moves within the model's pair and
can never change the model.

The hinge: cheap-model-plus-iterations is only permitted when a MACHINE can tell pass from
fail. Without a verifier, every retry costs a human review cycle, not just tokens.

Enforced here by hooks, not by your goodwill:
  - a turn that changes files cannot end until the project's checks have actually run
  - while a fix is in progress, the tests that gate it cannot be edited
  - every rung delegation is costed to .claude/arc/ledger.jsonl
"""


def h_sessionstart(data, root, cfg):
    """One injection per session. SessionStart is one of only three events whose stdout
    reaches the conversation, so this is the cheap place to put the doctrine -- once, not
    once per turn."""
    out = [DIGEST]

    checks = cfg.get("checks") or {}
    if checks:
        out.append("Checks for this project: " +
                   " | ".join("%s: %s" % (k, v) for k, v in checks.items()))

    errs = os.path.join(state_dir(root), "hook-errors.log")
    if os.path.exists(errs) and os.path.getsize(errs) > 0:
        try:
            with open(errs, encoding="utf-8") as f:
                last = f.readlines()[-3:]
            out.append("\n!! ARC hooks reported errors -- the gate may not be enforcing:\n" +
                       "".join("   " + l for l in last) +
                       "   (see .claude/arc/hook-errors.log)")
        except Exception:
            pass

    sys.stdout.write("\n".join(out))
    return 0


HANDLERS = {
    "pretooluse": h_pretooluse,
    "posttooluse": h_posttooluse,
    "stop": h_stop,
    "sessionstart": h_sessionstart,
    "userpromptsubmit": h_userpromptsubmit,
}


def main():
    event = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    handler = HANDLERS.get(event)
    if not handler:
        return 0

    data = read_input()
    root = project_root()
    if not root:
        return 0                                  # not opted in: do nothing at all

    try:
        return handler(data, root, load_config(root))
    except Exception as e:                        # noqa: BLE001
        # Fail open, but loudly: never wedge a session over a bug in this file.
        record_error(root, "%s handler: %r" % (event, e))
        sys.stderr.write("ARC hook '%s' errored and was skipped: %r\n" % (event, e))
        return 0


if __name__ == "__main__":
    sys.exit(main())
