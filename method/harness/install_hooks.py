#!/usr/bin/env python3
"""
Install (or remove) the ARC enforcement hooks in a Claude Code settings file.

Merges the hook definitions into the settings JSON without disturbing anything else, and is
idempotent: re-running replaces the ARC entries rather than stacking duplicates.

The hooks themselves no-op in any project without an `.arc` marker, so a global install is
safe -- nothing changes until you opt a project in.

    python install_hooks.py                 # into ~/.claude/settings.json
    python install_hooks.py --uninstall
    python install_hooks.py --project PATH  # into PATH/.claude/settings.json instead
    python install_hooks.py --dry-run

Deliberately Python rather than PowerShell: the merge is nested-JSON surgery, which
Windows PowerShell 5.1 does clumsily via PSCustomObject, and this same file works on a
Linux server unchanged.
"""

import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "arc_guard.py")
TEMPLATE = os.path.join(HERE, "hooks.json")
TAG = "arc_guard.py"          # how ARC's own entries are recognised on re-run / uninstall


def settings_path(project=None):
    if project:
        return os.path.join(os.path.abspath(project), ".claude", "settings.json")
    home = os.path.expanduser("~")
    return os.path.join(home, ".claude", "settings.json")


def load(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        return json.loads(text) if text else {}
    except Exception as e:                                    # noqa: BLE001
        print("ERROR: %s is not valid JSON (%s). Refusing to touch it." % (path, e))
        sys.exit(1)


def is_arc(entry):
    try:
        return any(TAG in str(h.get("command", "")) for h in entry.get("hooks", []))
    except Exception:                                         # noqa: BLE001
        return False


def strip_arc(settings):
    """Remove every previously installed ARC entry, leaving other hooks untouched."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks.keys()):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not is_arc(e)]
        removed += len(entries) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return removed


def build_entries():
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = json.load(f)
    raw = json.dumps(tpl["hooks"]).replace("__ARC_GUARD__", GUARD.replace("\\", "\\\\"))
    return json.loads(raw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--project", help="install into a project's .claude/settings.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(GUARD):
        print("ERROR: arc_guard.py not found next to this script (%s)" % GUARD)
        return 1

    path = settings_path(a.project)
    settings = load(path)
    removed = strip_arc(settings)

    if a.uninstall:
        action = "Removed %d ARC hook entr%s" % (removed, "y" if removed == 1 else "ies")
    else:
        incoming = build_entries()
        hooks = settings.setdefault("hooks", {})
        added = 0
        for event, entries in incoming.items():
            hooks.setdefault(event, [])
            hooks[event].extend(entries)
            added += len(entries)
        action = "Installed %d ARC hook entries%s" % (
            added, " (replacing %d existing)" % removed if removed else "")

    if a.dry_run:
        print("DRY RUN -- nothing written")
        print("  target : " + path)
        print("  " + action)
        print("\n" + json.dumps(settings.get("hooks", {}), indent=2)[:1400])
        return 0

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = path + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, backup)
        print("  backup : " + backup)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("  target : " + path)
    print("  " + action)
    if not a.uninstall:
        print("  guard  : " + GUARD)
        print("""
Nothing is enforced yet. To opt a project in:

    copy "%s" <project>\\.arc

then edit its `checks` to your real build/test/lint commands. Without that file the hooks
run and immediately exit -- no behaviour change anywhere.

Hooks are picked up on the next session start.""" % os.path.join(HERE, "arc.template.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
