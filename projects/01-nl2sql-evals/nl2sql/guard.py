"""Static SQL guard: read-only, single SELECT, allowed tables only, bounded result size.

Defence in depth. The guard is the first layer; the executor additionally opens
DuckDB read-only with external access disabled, so a guard bypass still cannot
write or read files.

Checks, in order:
  1. a single-pass tokenizer separates code, string literals, quoted identifiers and
     comments (so a literal like '--' cannot hide the rest of the query, and a
     comment cannot hide a string boundary); unterminated literals are rejected;
  2. at most one statement (DuckDB's own parser splits statements);
  3. the statement type is SELECT;
  4. no forbidden keywords in code or quoted identifiers (ATTACH, COPY, PRAGMA,
     SHOW, SUMMARIZE, DESCRIBE, ...), and no file/system functions;
  5. the AST (json_serialize_sql) references only allowed base tables or CTEs,
     and no table functions;
  6. the result size is bounded by wrapping: SELECT * FROM (<sql>) LIMIT n.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import duckdb

from nl2sql import config
from nl2sql.schema import ALLOWED_TABLES

FORBIDDEN = (
    "insert", "update", "delete", "merge", "drop", "create", "alter", "truncate",
    "attach", "detach", "copy", "export", "import", "pragma", "install", "load", "call",
    "set", "reset", "checkpoint", "vacuum", "grant", "revoke", "begin", "commit", "rollback",
    "use", "show", "summarize", "describe",
)
FORBIDDEN_FUNCS = ("current_setting", "getvariable", "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
                   "read_text", "read_blob", "glob", "parquet_scan", "sqlite_scan", "postgres_scan",
                   "getenv", "query_table", "query")

_DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


class TokenizeError(ValueError):
    pass


def tokenize(sql: str) -> list[tuple[str, str]]:
    """Single pass over the SQL. Returns (kind, text) with kind in
    code | string | ident | comment. String/ident text includes the delimiters."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(sql)
    buf_start = 0

    def flush(upto: int):
        if upto > buf_start:
            out.append(("code", sql[buf_start:upto]))

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            flush(i)
            j = sql.find("\n", i)
            j = n if j < 0 else j
            out.append(("comment", sql[i:j]))
            i = buf_start = j
            continue
        if ch == "/" and nxt == "*":
            flush(i)
            j = sql.find("*/", i + 2)
            if j < 0:
                raise TokenizeError("unterminated block comment")
            out.append(("comment", sql[i:j + 2]))
            i = buf_start = j + 2
            continue
        if ch == "'":
            # E'...' strings allow backslash escapes.
            escape = i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            flush(i)
            j = i + 1
            while True:
                if j >= n:
                    raise TokenizeError("unterminated string literal")
                if escape and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(("string", sql[i:j + 1]))
            i = buf_start = j + 1
            continue
        if ch == '"':
            flush(i)
            j = i + 1
            while True:
                if j >= n:
                    raise TokenizeError("unterminated quoted identifier")
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append(("ident", sql[i:j + 1]))
            i = buf_start = j + 1
            continue
        if ch == "$":
            m = _DOLLAR_RE.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, m.end())
                if j < 0:
                    raise TokenizeError("unterminated dollar-quoted string")
                flush(i)
                out.append(("string", sql[i:j + len(tag)]))
                i = buf_start = j + len(tag)
                continue
        i += 1
    flush(n)
    return out


@dataclass
class GuardResult:
    ok: bool
    sql: str = ""
    reasons: list[str] = field(default_factory=list)


_CTE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _walk(node, scope: frozenset, unknown: set[str], problems: list[str]) -> None:
    """Walk the AST. CTE names are scoped: a CTE is visible only inside the query node that
    defines it (and its subtree), never in an enclosing or sibling query."""
    if isinstance(node, dict):
        t = node.get("type")
        if t == "BASE_TABLE":
            name = str(node.get("table_name", "")).lower()
            if name not in ALLOWED_TABLES and name not in scope:
                unknown.add(name)
            if node.get("schema_name") or node.get("catalog_name"):
                problems.append("qualified table names are not allowed")
        elif t == "TABLE_FUNCTION":
            problems.append("table functions are not allowed")
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            names = set()
            for entry in cte_map.get("map", []) or []:
                key = entry.get("key") if isinstance(entry, dict) else None
                if key:
                    key = str(key).lower()
                    if not _CTE_NAME_RE.match(key):
                        problems.append(f"CTE name must be a plain identifier: {key!r}")
                    names.add(key)
            scope = scope | names
        for v in node.values():
            _walk(v, scope, unknown, problems)
    elif isinstance(node, list):
        for v in node:
            _walk(v, scope, unknown, problems)


def check(sql: str, max_rows: int = config.MAX_ROWS) -> GuardResult:
    if not isinstance(sql, str) or not sql.strip():
        return GuardResult(False, reasons=["empty SQL"])
    try:
        tokens = tokenize(sql)
    except TokenizeError as exc:
        return GuardResult(False, reasons=[str(exc)])

    # Keyword scan: code only. A quoted identifier is a name, so an alias such as "policies in use"
    # or "Show total" must not be blocked; statement type and the AST walk cover real statements.
    code_only = " ".join(t if k == "code" else " " for k, t in tokens).lower()
    # Function scan: code plus quoted identifiers, so "read_csv"(...) is still caught.
    scan = " ".join(t if k == "code" else t[1:-1] if k == "ident" else " "
                    for k, t in tokens).lower()
    # SQL with comments removed (strings kept verbatim); trailing semicolons dropped.
    clean = "".join(" " if k == "comment" else t for k, t in tokens).strip()
    while clean.endswith(";"):
        clean = clean[:-1].rstrip()
    if not clean:
        return GuardResult(False, reasons=["empty SQL"])

    words = set(re.findall(r"[a-z_]+", code_only))
    reasons = [f"forbidden keyword: {w.upper()}" for w in FORBIDDEN if w in words]
    reasons += [f"forbidden function: {f}" for f in FORBIDDEN_FUNCS if re.search(rf"\b{f}\s*\(", scan)]
    if reasons:
        return GuardResult(False, reasons=reasons)

    try:
        stmts = duckdb.extract_statements(clean)
    except Exception as exc:  # parse error
        return GuardResult(False, reasons=[f"parse error: {exc}"])
    if len(stmts) != 1:
        return GuardResult(False, reasons=[f"expected exactly 1 statement, got {len(stmts)}"])
    if stmts[0].type != duckdb.StatementType.SELECT:
        return GuardResult(False, reasons=[f"only SELECT is allowed, got {stmts[0].type.name}"])

    con = duckdb.connect(":memory:")
    try:
        ast = json.loads(con.execute("SELECT json_serialize_sql(?)", [clean]).fetchone()[0])
    finally:
        con.close()
    if ast.get("error"):
        return GuardResult(False, reasons=[f"parse error: {ast.get('error_message')}"])
    unknown: set[str] = set()
    problems: list[str] = []
    _walk(ast, frozenset(), unknown, problems)
    if unknown:
        problems.append(f"tables not allowed: {', '.join(sorted(unknown))}")
    if problems:
        return GuardResult(False, reasons=problems)

    # Bound the result size without parsing LIMIT/OFFSET/FETCH: wrap the whole query.
    wrapped = f"SELECT * FROM (\n{clean}\n) AS _guarded LIMIT {int(max_rows)}"
    return GuardResult(True, sql=wrapped)
