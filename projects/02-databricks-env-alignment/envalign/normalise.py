"""Normalisation and fingerprinting.

Two levels of equality are used everywhere:
  * identical  - equal after cosmetic normalisation (whitespace, case outside literals, key order, ids)
  * expected   - equal only after per-environment tokens / fields are masked (contoso_dev vs contoso_prod,
                 schedule pause status, cluster size ...). These are legitimate env differences, not drift.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

ENV_TOKEN_PATTERNS = [
    (re.compile(r"contoso([_-])(dev|uat|prod)\b", re.I), r"contoso\1<ENV>"),
    # only the bundle deployment path is an env path - not /dev/null or a Shared/dev tools folder
    (re.compile(r"(/bundles/[\w-]+)/(dev|uat|prod)/", re.I), r"\1/<ENV>/"),
    (re.compile(r"\b(dev|uat|prod)\.contoso-insurance\.example\b", re.I), "<ENV>.contoso-insurance.example"),
    (re.compile(r"-(dev|uat|prod)@", re.I), "-<ENV>@"),
]
_ENV_FINDERS = [re.compile(r"contoso[_-](dev|uat|prod)\b", re.I), re.compile(r"/bundles/[\w-]+/(dev|uat|prod)/", re.I),
                re.compile(r"\b(dev|uat|prod)\.contoso-insurance\.example\b", re.I),
                re.compile(r"-(dev|uat|prod)@", re.I)]


def env_tokens(text: str) -> set[str]:
    """Which environments a piece of text refers to (catalog names, paths, hosts, mailboxes)."""
    return {m.group(1).lower() for pat in _ENV_FINDERS for m in pat.finditer(text or "")}

# Job fields that legitimately differ per environment (dotted paths, '*' matches any list index / key).
EXPECTED_JOB_FIELDS = [
    "schedule.pause_status",          # allowed to differ, EXCEPT a paused job in prod (checked in envdiff)
    "continuous.pause_status",
    "job_clusters.*.new_cluster.num_workers",
    "job_id",
    "created_time",
]
# Volatile fields removed before even the "identical" comparison.
VOLATILE_JOB_FIELDS = ["job_id", "created_time"]

# Config tables whose *content* is environment specific by design.
EXPECTED_ENV_TABLES = frozenset({"core.cfg_environment"})

_VIEW_PREFIX = re.compile(
    r"^\s*create\s+(or\s+replace\s+)?(temp(orary)?\s+)?view\s+(if\s+not\s+exists\s+)?[\w.\"`]+\s+as\s+",
    re.I | re.S,
)
_LITERAL = re.compile(r"('[^']*(?:''[^']*)*'|\"[^\"]*(?:\"\"[^\"]*)*\"|`[^`]*(?:``[^`]*)*`)")
_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


def sha(obj) -> str:
    if not isinstance(obj, (str, bytes)):
        obj = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    if isinstance(obj, str):
        obj = obj.encode("utf-8")
    return hashlib.sha256(obj).hexdigest()


def env_mask(text: str) -> str:
    for pat, rep in ENV_TOKEN_PATTERNS:
        text = pat.sub(rep, text)
    return text


def env_substitute(text: str, src_env: str, dst_env: str) -> str:
    """Rewrite env tokens of src_env into dst_env (used when promoting job JSON)."""
    text = re.sub(rf"contoso([_-]){src_env}\b", rf"contoso\g<1>{dst_env}", text, flags=re.I)
    text = re.sub(rf"(/bundles/[\w-]+)/{src_env}/", rf"\g<1>/{dst_env}/", text, flags=re.I)
    text = re.sub(rf"-{src_env}@", f"-{dst_env}@", text, flags=re.I)
    text = re.sub(rf"\b{src_env}\.contoso-insurance\.example\b", f"{dst_env}.contoso-insurance.example", text, flags=re.I)
    return text


_TOKEN = re.compile(
    r"'[^']*(?:''[^']*)*'?"          # string literal (unterminated: runs to the end)
    r"|\"[^\"]*(?:\"\"[^\"]*)*\"?"   # double-quoted identifier
    r"|`[^`]*(?:``[^`]*)*`?"         # backticked identifier
    r"|--[^\n]*"                     # line comment
    r"|/\*(?:.*?\*/|.*\Z)",          # block comment (unterminated: runs to the end)
    re.S,
)


def strip_sql_comments(sql: str) -> str:
    """Remove comments; quotes and quoted identifiers are kept verbatim. Linear time (regex, no backtracking
    alternation per character), so a 1 MB body with unterminated quotes stays fast."""
    def keep(m):
        t = m.group(0)
        return " " if t.startswith(("--", "/*")) else t
    return _TOKEN.sub(keep, sql)


def view_body(sql: str) -> str:
    """Strip comments and the CREATE [OR REPLACE] VIEW [IF NOT EXISTS] name AS prefix."""
    s = strip_sql_comments(sql).strip().rstrip(";").strip()
    return _VIEW_PREFIX.sub("", s, count=1)


def norm_sql(sql: str, mask_env: bool = True) -> str:
    body = view_body(sql)
    parts = _LITERAL.split(body)
    norm = []
    for k, p in enumerate(parts):
        if k % 2 == 1:  # string literal or quoted identifier: keep case and whitespace
            inner = p[1:-1]
            qualified = (k > 0 and parts[k - 1].rstrip().endswith(".")) or \
                        (k + 1 < len(parts) and parts[k + 1].lstrip().startswith("."))
            if p[0] in "\"`" and qualified and re.fullmatch(r"[A-Za-z_]\w*", inner):
                p = inner.lower()  # object-name part (core."POLICY"): name resolution is case-insensitive
            elif p[0] in "\"`" and _PLAIN_IDENT.match(inner):
                p = inner  # "policy" == policy; a column alias "Amount" stays quoted and case-sensitive
            elif p[0] == "`":
                p = '"' + p[1:-1] + '"'
            norm.append(p)
        else:
            p = re.sub(r"\s+", " ", p.lower())
            p = re.sub(r"\s*([(),=<>+\-*/])\s*", r"\1", p)
            norm.append(p)
    out = "".join(norm).strip()
    return env_mask(out) if mask_env else out


def _drop_path(obj, parts: list[str]):
    if not parts or obj is None:
        return
    head, rest = parts[0], parts[1:]
    if head == "*":
        items = obj if isinstance(obj, list) else (obj.values() if isinstance(obj, dict) else [])
        for it in items:
            if rest:
                _drop_path(it, rest)
        return
    if isinstance(obj, dict) and head in obj:
        if rest:
            _drop_path(obj[head], rest)
        else:
            del obj[head]


def _env_mask_json(obj):
    if isinstance(obj, dict):
        return {k: _env_mask_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_env_mask_json(v) for v in obj]
    if isinstance(obj, str):
        return env_mask(obj)
    return obj


def _sort_lists(obj):
    """Sort lists of task-like dicts by task_key / job_cluster_key so ordering is not drift."""
    if isinstance(obj, dict):
        return {k: _sort_lists(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        items = [_sort_lists(v) for v in obj]
        if items and all(isinstance(v, dict) for v in items):
            for key in ("task_key", "job_cluster_key", "name"):
                if all(key in v for v in items):
                    return sorted(items, key=lambda v: str(v[key]))
        return items
    return obj


def norm_job(job: dict, level: str = "identical") -> dict:
    j = copy.deepcopy(job)
    fields = VOLATILE_JOB_FIELDS if level == "identical" else EXPECTED_JOB_FIELDS
    for f in fields:
        _drop_path(j, f.split("."))
    if level != "identical":
        j = _env_mask_json(j)
    return _sort_lists(j)


def json_diff_paths(a, b, prefix: str = "") -> list[str]:
    if type(a) is not type(b):
        return [prefix or "<root>"]
    if isinstance(a, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in a or k not in b:
                out.append(p)
            else:
                out += json_diff_paths(a[k], b[k], p)
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [prefix or "<root>"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += json_diff_paths(x, y, f"{prefix}[{i}]")
        return out
    return [] if a == b else [prefix or "<root>"]


def row_hash(row: dict, mask_env: bool = False) -> str:
    vals = {k: (None if v is None else str(v)) for k, v in row.items()}
    if mask_env:
        vals = {k: (env_mask(v) if isinstance(v, str) else v) for k, v in vals.items()}
    return sha(vals)
