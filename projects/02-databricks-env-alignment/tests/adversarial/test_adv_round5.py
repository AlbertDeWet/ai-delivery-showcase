"""Adversarial tests, Round 5 (role: ADVERSARY). Final focused attack on the parse-tree sqlguard.

Must-block cases are first proven to run on DuckDB AND to read team_b; must-allow cases are proven to run.
In-memory and fast.
"""
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from envalign.sqlguard import statement_problems  # noqa: E402


def _duck():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA core; CREATE SCHEMA team_b; CREATE TABLE team_b.ingest_log AS SELECT 42 AS x;"
              "CREATE TABLE core.claim AS SELECT 1 AS claim_id, 'open' AS status;"
              "CREATE TABLE core.cfg_x AS SELECT '42' AS key")
    return c


MUST_BLOCK = [  # (statement, item object) - every one runs on DuckDB and touches team_b
    ("CREATE VIEW core.v1 AS SELECT (SELECT max(x) FROM team_b.ingest_log) AS m", "core.v1"),
    ("CREATE VIEW core.v3 AS SELECT * FROM core.claim c, LATERAL (SELECT x FROM team_b.ingest_log) t", "core.v3"),
    ("CREATE VIEW core.a1 AS SUMMARIZE team_b.ingest_log", "core.a1"),
    ("CREATE VIEW core.a2 AS DESCRIBE team_b.ingest_log", "core.a2"),
    ("CREATE VIEW core.a3 AS TABLE team_b.ingest_log", "core.a3"),
    ("CREATE VIEW core.a4 AS SELECT * FROM core.claim POSITIONAL JOIN team_b.ingest_log", "core.a4"),
    ("CREATE VIEW core.a5 AS (SELECT x FROM team_b.ingest_log)", "core.a5"),
    ("CREATE VIEW core.a6 AS SELECT * FROM team_b.ingest_log AS core", "core.a6"),
    ("CREATE VIEW core.a8 AS SELECT * FROM (FROM team_b.ingest_log)", "core.a8"),
    ("CREATE VIEW core.a9 AS SELECT * FROM core.claim UNION BY NAME FROM team_b.ingest_log", "core.a9"),
    ("CREATE VIEW core.v12 AS FROM team_b.ingest_log SELECT x", "core.v12"),
    ("CREATE VIEW core.v13 AS SELECT 1 AS x UNION ALL SELECT x FROM team_b.ingest_log", "core.v13"),
    ("CREATE VIEW core.v15 AS SELECT * FROM \"TEAM_B\".\"INGEST_LOG\"", "core.v15"),
    ("DELETE FROM core.cfg_x USING team_b.ingest_log t WHERE CAST(t.x AS VARCHAR) = key", "core.cfg_x"),
    ("DELETE FROM core.cfg_x WHERE key IN (SELECT CAST(x AS VARCHAR) FROM team_b.ingest_log)", "core.cfg_x"),
    ("INSERT INTO core.cfg_x SELECT CAST(x AS VARCHAR) FROM team_b.ingest_log", "core.cfg_x"),
    ("INSERT INTO core.cfg_x VALUES ((SELECT CAST(max(x) AS VARCHAR) FROM team_b.ingest_log))", "core.cfg_x"),
]


@pytest.mark.parametrize("stmt,obj", MUST_BLOCK, ids=[o + ":" + s.split()[0] + str(i) for i, (s, o) in
                                                       enumerate(MUST_BLOCK)])
def test_parse_tree_guard_blocks_protected_reads(stmt, obj):
    """ADVERSARY: subqueries, LATERAL, SUMMARIZE/DESCRIBE/TABLE, POSITIONAL JOIN, FROM-first, UNION BY NAME,
    an alias named 'core', upper-case quoted TEAM_B, DELETE ... USING / IN (subquery), INSERT ... SELECT and a
    scalar subquery in VALUES: each runs on DuckDB and reaches team_b, and each must be blocked."""
    _duck().execute(stmt)
    assert statement_problems(stmt, obj), stmt


@pytest.mark.parametrize("stmt,obj", [
    ("CREATE VIEW core.c1 AS SELECT * FROM \"CORE\".\"CLAIM\"", "core.c1"),
    ("CREATE VIEW CORE.C2 AS SELECT * FROM Core.Claim", "core.c2"),
    ("CREATE VIEW core.c3 AS WITH claim AS (SELECT * FROM core.claim) SELECT * FROM claim", "core.c3"),
    ("CREATE VIEW core.c4 AS SELECT * FROM core.claim c, LATERAL (SELECT c.claim_id + 1 AS n) t", "core.c4"),
    ("DELETE FROM core.cfg_x WHERE key IN (SELECT CAST(claim_id AS VARCHAR) FROM core.claim)", "core.cfg_x"),
])
def test_parse_tree_guard_allows_legit_core_sql(stmt, obj):
    """ADVERSARY: schema-name case/quoting of CORE, a CTE shadowing a core table, a LATERAL over core and a
    DELETE with a core subquery are legitimate and must pass (no false block)."""
    _duck().execute(stmt)
    assert statement_problems(stmt, obj) == []


def test_pivot_is_rejected_by_duckdb_itself():
    """ADVERSARY: PIVOT with data-derived columns cannot be used in a view at all - DuckDB refuses it, and the
    guard reports the parse failure (fail-closed, no bypass)."""
    s = "CREATE VIEW core.p AS SELECT * FROM (PIVOT team_b.ingest_log ON x USING count(*))"
    with pytest.raises(duckdb.Error):
        _duck().execute(s)
    assert statement_problems(s, "core.p")


def test_cte_named_like_protected_table_cannot_reach_it():
    """ADVERSARY: a CTE named ingest_log defined in one scope and referenced unqualified OUTSIDE it is accepted by
    the guard (CTE names are collected tree-wide, not per scope), but DuckDB binds the unqualified name to the
    view's own schema, never to team_b - so this is not a bypass (LOW: guard scope is imprecise)."""
    s = ("CREATE VIEW core.s1 AS SELECT * FROM (WITH ingest_log AS (SELECT 1 AS x) SELECT x FROM ingest_log) a, "
         "ingest_log")
    with pytest.raises(duckdb.Error):
        _duck().execute(s)  # unqualified ingest_log does not resolve to team_b.ingest_log


@pytest.mark.xfail(strict=True, reason="K2 LOW open: CTE names collected tree-wide, not per scope; DuckDB binds the stray name to the view's own schema, so no protected reach")
def test_cte_scope_is_checked_per_scope():
    """ADVERSARY (LOW): the same statement should be rejected by the guard itself, because the outer
    'ingest_log' is not a CTE in its scope."""
    s = ("CREATE VIEW core.s1 AS SELECT * FROM (WITH ingest_log AS (SELECT 1 AS x) SELECT x FROM ingest_log) a, "
         "ingest_log")
    assert statement_problems(s, "core.s1")


@pytest.mark.xfail(strict=True, reason="K1 LOW open: a quoted dotted name is canonicalised to schema.name; the object lands in main; T2 should then fail and roll back (not verified)")
def test_quoted_dotted_name_is_not_the_item_object():
    """ADVERSARY (LOW): CREATE VIEW "core.v8" creates main."core.v8" (one identifier containing a dot), but the
    guard canonicalises it to core.v8 and accepts it as the item's object."""
    c = _duck()
    c.execute('CREATE VIEW "core.v8" AS SELECT 1 AS a')
    assert c.execute("SELECT schema_name FROM duckdb_views() WHERE view_name = 'core.v8'").fetchone()[0] == "main"
    assert statement_problems('CREATE VIEW "core.v8" AS SELECT 1 AS a', "core.v8")


def test_money_string_with_dollar_is_blocked_and_documented():
    """ADVERSARY: '$5' in a literal is blocked by the fail-closed '$' rule. Accepted as fail-safe (a false block
    cannot leak data) PROVIDED the README limitations say so - which this test pins."""
    s = "CREATE VIEW core.m AS SELECT '$5' AS price"
    _duck().execute(s)
    assert statement_problems(s, "core.m")
    readme = (PROJECT / "README.md").read_text(encoding="utf-8")
    assert "fails closed" in readme and "`$`" in readme
