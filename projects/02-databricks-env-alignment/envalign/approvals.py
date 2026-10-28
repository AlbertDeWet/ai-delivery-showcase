"""Approvals ledger, kept OUTSIDE the plan file.

When `promote` writes a plan for review, it appends an entry to <root>/_approvals/ledger.jsonl:
    token, plan_hash, lint_inputs_hash (target job definitions/tags + view definitions), target,
    mac = HMAC-SHA256(local secret, plan_hash | lint_inputs_hash | target)
The secret lives in .envalign/secret.key (gitignored, generated on first use). `apply` recomputes the plan hash
and the lint-input hash and accepts the human's token only if a ledger entry with a valid MAC matches both.
Editing the plan file, rewriting its stored token, or changing the jobs/views lint read all break the match.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .workspace import DEFAULT_ROOT, PROJECT_ROOT

SECRET_PATH = PROJECT_ROOT / ".envalign" / "secret.key"


class SecretMissing(Exception):
    pass


def _secret(create: bool = False) -> bytes:
    """Only the issuer (`record`) may create the key. A verifier never mints key material."""
    if not SECRET_PATH.exists():
        if not create:
            raise SecretMissing(str(SECRET_PATH))
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


def ledger_path(root=None) -> Path:
    return Path(root or DEFAULT_ROOT) / "_approvals" / "ledger.jsonl"


def _mac(plan_hash: str, inputs_hash: str, target: str, create: bool = False, kind: str = "issued") -> str:
    msg = f"{plan_hash}|{inputs_hash}|{target}"
    if kind != "issued":
        msg = f"{kind}|{msg}"
    return hmac.new(_secret(create), msg.encode(), hashlib.sha256).hexdigest()


def _append(entry: dict, root=None) -> None:
    lp = ledger_path(root)
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def record(plan: dict, inputs_hash: str, root=None) -> dict:
    from .promote import plan_hash, plan_token
    ph = plan_hash(plan)
    entry = {"token": plan_token(plan), "plan_id": plan.get("plan_id"), "plan_hash": ph,
             "lint_inputs_hash": inputs_hash, "target": plan.get("target"),
             "mac": _mac(ph, inputs_hash, str(plan.get("target")), create=True),
             "recorded_utc": datetime.now(timezone.utc).isoformat()}
    _append(entry, root)
    return entry


def consume(plan: dict, inputs_hash: str, root=None) -> None:
    """Mark an approval as used (single use). Written BEFORE the change starts, so a crash mid-apply cannot
    leave a reusable approval behind."""
    from .promote import plan_hash
    ph, target = plan_hash(plan), str(plan.get("target"))
    _append({"consumed": True, "plan_hash": ph, "lint_inputs_hash": inputs_hash, "target": target,
             "mac": _mac(ph, inputs_hash, target, kind="consumed"),
             "recorded_utc": datetime.now(timezone.utc).isoformat()}, root)


def verify(plan: dict, inputs_hash: str, root=None) -> str | None:
    """None if the plan (as it is now) and the lint inputs (as they are now) match a genuine, unused entry."""
    from .promote import plan_hash, plan_token
    lp = ledger_path(root)
    if not lp.exists():
        return "no approvals ledger - the plan was not issued by `envalign plan`"
    ph, tok, target = plan_hash(plan), plan_token(plan), str(plan.get("target"))
    entries = []
    for line in lp.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    try:
        genuine = [e for e in entries if hmac.compare_digest(str(e.get("mac", "")), _mac(
            str(e.get("plan_hash")), str(e.get("lint_inputs_hash")), str(e.get("target")),
            kind="consumed" if e.get("consumed") else "issued"))]
    except SecretMissing as e:
        return f"HMAC secret missing ({e}); approvals cannot be verified - restore the secret or re-issue the plan"
    if any(e.get("consumed") and e.get("plan_hash") == ph and e.get("target") == target for e in genuine):
        return "this approval was already used (single use) - re-plan and re-review"
    same_plan = [e for e in genuine if not e.get("consumed") and e.get("plan_hash") == ph
                 and e.get("token") == tok and e.get("target") == target]
    if not same_plan:
        return "plan content does not match any plan issued for review (edited after review?)"
    if not any(e.get("lint_inputs_hash") == inputs_hash for e in same_plan):
        return "target job definitions/tags or views changed since the plan was reviewed - re-plan and re-review"
    return None
