"""ADVERSARY: DuckDB-specific attacks on the static guard (nl2sql/guard.py)."""

import pytest

from nl2sql import executor, guard

CTE_BYPASS = "SELECT * FROM 'f.csv', (WITH \"f.csv\" AS (SELECT 1) SELECT * FROM \"f.csv\") t"


@pytest.mark.parametrize("sql", [
    "SELECT * FROM 'claims.csv'",
    "SELECT * FROM \"/etc/passwd\"",
    "SELECT * FROM 's3://bucket/x.parquet'",
    "SELECT * FROM 'https://example.com/x.csv'",
    "SELECT * FROM policies p JOIN 'x.parquet' q ON true",
    "SELECT * FROM json_execute_serialized_sql(json_serialize_sql('SELECT 1'))",
    "SELECT * FROM \"read_text\"('a')",
    "SELECT * FROM read_csv /*x*/ ('a.csv')",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM duckdb_tables",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM claims, LATERAL (SELECT * FROM generate_series(1,2)) g",
    "PIVOT claims ON status USING count(*)",
])
def test_quoted_paths_and_table_functions_blocked(sql):
    """ADVERSARY: file paths as quoted tables, table functions and catalog views must be blocked."""
    assert not guard.check(sql).ok


@pytest.mark.parametrize("sql,expected", [
    ("SELECT $$ ; DROP TABLE x $$ AS v", (" ; DROP TABLE x ",)),
    ("SELECT $a$x$a$ AS v", ("x",)),
    ("SELECT 1 /* /* nested */ AS v", (1,)),
    ("SELECT E'\\'' AS v, 1 -- '", ("'", 1)),
    ("SELECT 'a\\' AS v, 1 AS w -- '", ("a\\", 1)),
])
def test_tokenizer_agrees_with_duckdb_on_dollar_strings_and_comments(sql, expected, con):
    """ADVERSARY: $$ / $tag$ strings, non-nesting comments and E'' escapes: the guard's view must
    match what DuckDB executes (one value, nothing hidden)."""
    g = guard.check(sql)
    assert g.ok
    _, rows = executor.run(g.sql, con)
    assert rows == [expected]


def test_nested_comment_close_is_rejected_not_executed():
    """ADVERSARY: DuckDB does not nest comments; a second */ must be a parse error, not hidden code."""
    assert not guard.check("SELECT 1 /* /* nested */ */ AS v").ok


def test_cte_in_subquery_does_not_whitelist_a_file_path_elsewhere():
    """ADVERSARY: CTE names are collected globally, ignoring scope. A CTE named like a file inside one
    subquery lets the same quoted file path through as a table in the outer query."""
    assert not guard.check(CTE_BYPASS).ok


def test_executor_still_stops_the_cte_scope_bypass(con):
    """ADVERSARY: defence in depth - even when the guard passes the bypass, the executor refuses file access."""
    g = guard.check(CTE_BYPASS)
    if g.ok:
        with pytest.raises(Exception, match="(?i)file system operations are disabled|permission"):
            executor.run(g.sql, con)


@pytest.mark.parametrize("sql", [
    "SELECT current_setting('memory_limit') AS v",
    "SELECT getvariable('x') AS v",
])
def test_scalar_config_introspection_is_blocked(sql):
    """ADVERSARY: scalar functions that read engine configuration are not in FORBIDDEN_FUNCS."""
    assert not guard.check(sql).ok


@pytest.mark.parametrize("sql", [
    'SELECT COUNT(*) AS "policies in use" FROM policies',
    'SELECT COUNT(*) AS "Show total" FROM policies',
])
def test_english_words_in_quoted_alias_are_not_false_blocked(sql):
    """ADVERSARY: the keyword scan reads inside quoted identifiers, so an innocent alias containing
    'use' or 'show' blocks a correct answer (graded as wrong)."""
    assert guard.check(sql).ok
