#!/usr/bin/env python3
"""Block commits that contain denylisted terms or email addresses (whole monorepo: method/, projects/*, scripts/, .github/).

The denylist lives in an UNTRACKED file, denylist.txt (gitignored), one term per
line; blank lines and lines starting with # are ignored. Matching is
case-insensitive substring; backslashes and forward slashes are treated alike, and repeated
separators (including JSON-escaped backslashes) are collapsed.

Modes
  python scripts/leakcheck.py               scan tracked + untracked, non-ignored files
  python scripts/leakcheck.py --staged      scan the staged versions of staged files (pre-commit)
  python scripts/leakcheck.py --commit-msg F   scan a commit message file (commit-msg hook)
  python scripts/leakcheck.py --paths a b   scan specific files

Fails closed: if denylist.txt is missing, exit 2 (unless --allow-missing-denylist).
Exit 0 = clean, 1 = hits found, 2 = configuration error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Allowed: RFC 2606 reserved example domains, e.g. x@example.com or x@contoso-insurance.example
EMAIL_ALLOW = re.compile(r"(@example\.(com|org|net)|\.example)$", re.I)
# Attribution lines the owner never wants in commits or files.
# (Built from pieces so this file does not match its own pattern.)
ATTRIBUTION_RE = re.compile("co" + r"-authored-by\s*:|generated with \[?" + "claude", re.I)
SKIP_NAMES = {"denylist.txt"}  # any denylist.txt, at the root or inside a project


def _norm(s: str) -> str:
    """Lower-case; treat backslashes and forward slashes alike; collapse repeated separators.

    Collapsing means a JSON-escaped Windows path (C:\\Users\\x) and a doubled slash
    (C://Users//x) both normalise to c:/users/x, so they match a term written either way.
    """
    s = s.lower().replace(r"\u005c", "/").replace("\\", "/")
    return re.sub(r"/{2,}", "/", s)


def load_terms(path: Path) -> list[str]:
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(_norm(line))
    return terms


def scan_text(text: str, terms: list[str]) -> list[str]:
    hits = []
    low = _norm(text)
    for t in terms:
        if t in low:
            hits.append(f"denylisted term #{terms.index(t) + 1}")
    for m in EMAIL_RE.finditer(text):
        if not EMAIL_ALLOW.search(m.group(0)):
            hits.append("email address")
    if ATTRIBUTION_RE.search(text):
        hits.append("attribution line")
    return sorted(set(hits))


def identity_emails(environ=None) -> list[str]:
    """Author/committer emails git will record: env overrides first, then git config."""
    import os

    environ = os.environ if environ is None else environ
    emails = [environ.get(k, "") for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")]
    r = subprocess.run(["git", "config", "--get", "user.email"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode == 0:
        emails.append(r.stdout.strip())
    return [e for e in emails if e]


def identity_hits(emails: list[str], terms: list[str]) -> list[str]:
    """Return the denylisted identities (reported by position, not value)."""
    hits = []
    for i, e in enumerate(emails):
        low = _norm(e)
        if any(t in low for t in terms if t):
            hits.append(f"author/committer email #{i + 1} is denylisted")
    return hits


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True,
                          encoding="utf-8").stdout


def _read_bytes(path: str, staged: bool) -> bytes | None:
    if staged:
        r = subprocess.run(["git", "show", f":{path}"], cwd=ROOT, capture_output=True)
        return r.stdout if r.returncode == 0 else None
    p = ROOT / path
    return p.read_bytes() if p.is_file() else None


def scan_files(paths: list[str], terms: list[str], staged: bool = False) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for rel in paths:
        if Path(rel).name in SKIP_NAMES:
            continue
        hits = [f"path: {h}" for h in scan_text(rel, terms) if h != "attribution line"]
        data = _read_bytes(rel, staged)
        if data is not None and b"\0" not in data[:8000]:
            hits += scan_text(data.decode("utf-8", errors="replace"), terms)
        if hits:
            found[rel] = hits
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--staged", action="store_true")
    ap.add_argument("--commit-msg")
    ap.add_argument("--paths", nargs="*")
    ap.add_argument("--denylist", default=str(ROOT / "denylist.txt"))
    ap.add_argument("--allow-missing-denylist", action="store_true")
    args = ap.parse_args(argv)

    deny = Path(args.denylist)
    if deny.exists():
        terms = load_terms(deny)
    elif args.allow_missing_denylist:
        terms = []
    else:
        print(f"leakcheck: {deny} not found; refusing to pass (fail closed).", file=sys.stderr)
        return 2

    if args.commit_msg or args.staged:
        id_hits = identity_hits(identity_emails(), terms)
        if id_hits:
            print("leakcheck: commit blocked: " + ", ".join(id_hits)
                  + ". Set a safe repo-local identity with git config --local user.email.", file=sys.stderr)
            return 1

    if args.commit_msg:
        text = Path(args.commit_msg).read_text(encoding="utf-8", errors="replace")
        text = "\n".join(l for l in text.splitlines() if not l.startswith("#"))
        hits = scan_text(text, terms)
        if hits:
            print("leakcheck: commit message blocked: " + ", ".join(hits), file=sys.stderr)
            return 1
        return 0

    if args.paths:
        paths = [str(Path(p).resolve().relative_to(ROOT)) if Path(p).is_absolute() else p for p in args.paths]
    elif args.staged:
        paths = [p for p in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if p]
    else:
        paths = [p for p in _git("ls-files", "-co", "--exclude-standard").splitlines() if p]

    found = scan_files(paths, terms, staged=args.staged)
    if found:
        print("leakcheck: BLOCKED. Remove these before committing:", file=sys.stderr)
        for f, hits in sorted(found.items()):
            print(f"  {f}: {', '.join(hits)}", file=sys.stderr)
        return 1
    print(f"leakcheck: clean ({len(paths)} files scanned, {len(terms)} denylist terms).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
