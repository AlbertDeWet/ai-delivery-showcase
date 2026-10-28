#!/usr/bin/env python3
"""Install the pre-commit and commit-msg hooks into .git/hooks."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    git_dir = Path(subprocess.run(["git", "rev-parse", "--git-dir"], cwd=ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip())
    hooks = (ROOT / git_dir if not git_dir.is_absolute() else git_dir) / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("pre-commit", "commit-msg"):
        dst = hooks / name
        shutil.copyfile(ROOT / "scripts" / name, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"installed {dst}")
    if not (ROOT / "denylist.txt").exists():
        print("WARNING: denylist.txt is missing; the hooks will refuse every commit until you create it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
