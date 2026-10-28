import pytest

from nl2sql import executor, guard

BLOCKED = [
    "DROP TABLE policies",
    "DELETE FROM claims",
    "UPDATE claims SET status = 'Closed'",
    "INSERT INTO brokers VALUES (99, 'x', 'North', 0.1)",
    "ATTACH 'other.db' AS o",
    "COPY claims TO 'out.csv'",
    "CREATE TABLE t AS SELECT * FROM claims",
    "ALTER TABLE claims ADD COLUMN x INT",
    "TRUNCATE claims",
    "PRAGMA database_list",
    "INSTALL httpfs",
    "SET threads = 1",
    "SELECT 1; DROP TABLE claims",
    "SELECT * FROM claims; SELECT * FROM policies",
    "SELECT * FROM read_csv('secrets.csv')",
    "SELECT * FROM 'data.parquet'",
    "SELECT * FROM salaries",
    "SELECT * FROM information_schema.tables",
    "WITH x AS (SELECT 1) DELETE FROM claims",
    "",
]


@pytest.mark.parametrize("sql", BLOCKED)
def test_blocks(sql):
    assert not guard.check(sql).ok


def test_allows_simple_select():
    r = guard.check("SELECT COUNT(*) FROM policies")
    assert r.ok and r.sql.rstrip().endswith("LIMIT 1000")


def test_allows_cte():
    r = guard.check("WITH a AS (SELECT * FROM claims) SELECT COUNT(*) FROM a")
    assert r.ok, r.reasons


def test_keyword_inside_string_literal_is_allowed():
    r = guard.check("SELECT COUNT(*) FROM claims WHERE description LIKE '%drop table%'")
    assert r.ok, r.reasons


def test_existing_small_limit_kept(con):
    r = guard.check("SELECT * FROM claims LIMIT 5;")
    assert r.ok and len(con.execute(r.sql).fetchall()) == 5


def test_large_limit_capped(con):
    r = guard.check("SELECT * FROM claims LIMIT 999999", max_rows=100)
    assert r.ok and len(con.execute(r.sql).fetchall()) == 100


def test_limit_is_enforced_by_wrapping():
    r = guard.check("SELECT * FROM claims", max_rows=7)
    assert r.ok and r.sql.startswith("SELECT * FROM (") and r.sql.endswith("LIMIT 7")


# Review item 3: these forms all broke or bypassed the old regex-based LIMIT handling.
LIMIT_FORMS = {
    "trailing_line_comment": ("SELECT * FROM claims -- all claims", 50),
    "trailing_comment_after_semicolon": ("SELECT * FROM claims; -- done", 50),
    "limit_offset": ("SELECT * FROM claims ORDER BY claim_id LIMIT 10 OFFSET 5", 10),
    "offset_only": ("SELECT * FROM claims ORDER BY claim_id OFFSET 5", 50),
    "limit_all": ("SELECT * FROM claims LIMIT ALL", 50),
    "fetch_first": ("SELECT * FROM claims ORDER BY claim_id FETCH FIRST 3 ROWS ONLY", 3),
    "limit_inside_cte": ("WITH a AS (SELECT * FROM claims LIMIT 4) SELECT * FROM a", 4),
    "block_comment_tail": ("SELECT * FROM claims /* trailing note */", 50),
}


@pytest.mark.parametrize("name", sorted(LIMIT_FORMS))
def test_limit_forms_execute_and_are_capped(name, con):
    sql, expected_rows = LIMIT_FORMS[name]
    r = guard.check(sql, max_rows=50)
    assert r.ok, r.reasons
    assert len(con.execute(r.sql).fetchall()) == expected_rows


# Review item 6: string and comment tokenising must happen in one pass.
def test_dash_dash_inside_string_does_not_hide_rest():
    assert not guard.check("SELECT '--', getenv('HOME')").ok
    assert not guard.check("SELECT '--' AS a FROM claims; DROP TABLE claims").ok


def test_block_comment_opener_inside_string_does_not_hide_rest():
    assert not guard.check("SELECT '/*' AS a, getenv('HOME') AS b, '*/' AS c").ok


def test_quote_inside_comment_does_not_open_string():
    assert not guard.check("SELECT 1 -- it's fine\n, getenv('HOME')").ok


def test_unterminated_string_rejected():
    assert not guard.check("SELECT 'abc FROM claims").ok


def test_quoted_identifier_cannot_hide_function():
    assert not guard.check("SELECT * FROM \"read_csv\"('x.csv')").ok


def test_dollar_quoted_string_is_a_literal():
    r = guard.check("SELECT $$drop table claims$$ AS note")
    assert r.ok, r.reasons


def test_tokenizer_single_pass_kinds():
    kinds = [k for k, _ in guard.tokenize("SELECT '--x' /* c */ , \"col\" -- end")]
    assert kinds == ["code", "string", "code", "comment", "code", "ident", "code", "comment"]


# Review item 7: these parse as SELECT in DuckDB, so they need explicit rejection.
@pytest.mark.parametrize("sql", ["SHOW TABLES", "SHOW claims", "SUMMARIZE claims", "DESCRIBE claims",
                                 "SELECT * FROM (SUMMARIZE claims)", "SELECT * FROM (DESCRIBE claims)",
                                 "WITH s AS (SUMMARIZE claims) SELECT * FROM s"])
def test_show_summarize_describe_rejected(sql):
    assert not guard.check(sql).ok


def test_executor_connection_is_read_only(con):
    with pytest.raises(Exception):
        con.execute("DELETE FROM claims")


def test_executor_blocks_file_access(con):
    with pytest.raises(Exception):
        con.execute("SELECT * FROM read_csv('nonexistent.csv')")


# Review item 8: executor resource caps (timeout is kept).
def test_executor_memory_limit_and_threads(con):
    assert con.execute("SELECT current_setting('threads')").fetchone()[0] == 2
    mem = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    assert mem == "953.6 MiB", mem  # DuckDB reports 1GB (10^9 bytes) in MiB


def test_executor_timeout_interrupts(con):
    from nl2sql import executor

    with pytest.raises(Exception):
        executor.run("SELECT COUNT(*) FROM range(100000000000) a", con, timeout_s=0.5)
