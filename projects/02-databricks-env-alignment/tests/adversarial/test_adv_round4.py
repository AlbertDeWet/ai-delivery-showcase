"""Adversarial tests, Round 4 (role: ADVERSARY). Attacks envalign/sqlguard.py.

Each bypass test proves BOTH that DuckDB really executes the statement (reaching team_b) and that the linter lets
it through. A failing test is an open finding. All in-memory, fast.
"""
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from envalign.promote import lint  # noqa: E402
from envalign.sqlguard import statement_problems  # noqa: E402


def _duck():
    c = duckdb.connect()
    c.execute("CREATE SCHEMA core; CREATE SCHEMA team_b; CREATE TABLE team_b.ingest_log AS SELECT 42 AS x")
    c.execute("CREATE TABLE core.claim AS SELECT 1 AS claim_id, 100.0 AS amount, 'open' AS status, "
              "DATE '2026-01-01' AS reported_date")
    return c


def _lint_one(tmp_path, obj, stmt):
    root = tmp_path / "ws"
    (root / "uat" / "jobs").mkdir(parents=True, exist_ok=True)
    it = {"id": "001", "kind": "view", "object": obj, "action": "create_view", "change": [stmt],
          "rollback": [f"DROP VIEW IF EXISTS {obj}"], "backup": {"type": "ddl_capture", "object": obj}}
    return lint({"plan_id": "adv4", "source": "dev", "target": "uat", "items": [it]}, root)


BYPASSES = {
    # $$...$$ is a DuckDB string; the guard does not know it, so a ' inside it opens a fake string that swallows
    # the real query(...) call.
    "dollar_quote": "CREATE VIEW core.v_d AS SELECT $$'$$ AS a, * FROM query('SELECT * FROM team' || "
                    "'_b.ingest_log') t, (SELECT '$$') u",
    # DuckDB nests block comments; the guard ends the comment at the first */, so a ' after it opens a fake
    # string that hides the call, closed by a ' inside a trailing line comment.
    "nested_comment": "CREATE VIEW core.v_n AS SELECT * /* /* */ ' */ FROM query('SELECT * FROM team' || "
                      "'_b.ingest_log') --'",
}


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_sqlguard_tokenizer_desync_reaches_team_b(tmp_path, name):
    """ADVERSARY: a tokenizer mismatch between sqlguard and DuckDB (dollar-quoted strings, nested block comments)
    hides query('...team' || '_b...') from the guard; DuckDB runs it and returns the protected team's rows.
    The linter must block it."""
    stmt = BYPASSES[name]
    obj = stmt.split()[2]
    c = _duck()
    c.execute(stmt)
    assert c.execute(f"SELECT * FROM {obj}").fetchall(), "precondition: DuckDB executes it"
    f = _lint_one(tmp_path, obj, stmt)
    assert any(x["rule"] == "R2" and x["blocking"] for x in f), f"no blocking R2: {f}"


def test_sqlguard_replacement_scan_reads_local_files(tmp_path):
    """ADVERSARY: FROM '<path>' is a DuckDB replacement scan = read_json/read_csv without a function call. The
    guard denies read_* functions ('reads outside the catalog') but lets a view read any local file, e.g. the
    approvals ledger or another team's job JSON via a glob."""
    f = tmp_path / "ledger.jsonl"
    f.write_text('{"mac": "abc"}\n', encoding="utf-8")
    stmt = f"CREATE VIEW core.v_r AS SELECT * FROM '{f.as_posix()}'"
    c = _duck()
    c.execute(stmt)
    assert c.execute("SELECT * FROM core.v_r").fetchall() == [("abc",)]
    assert statement_problems(stmt, "core.v_r"), "file read via replacement scan not flagged"


def test_homoglyph_function_name_is_harmless():
    """ADVERSARY: a full-width 'query' is not a DuckDB function, so whatever the guard says it cannot run.
    (Baseline: DuckDB rejects it.)"""
    with pytest.raises(duckdb.Error):
        _duck().execute("CREATE VIEW core.v_h AS SELECT * FROM ｑｕｅｒｙ('SELECT 1')")


def test_homoglyph_keyword_is_harmless():
    """ADVERSARY: a Cyrillic 'е' in EXECUTE is not a keyword to DuckDB either (baseline)."""
    with pytest.raises(duckdb.Error):
        _duck().execute("ЕXЕCUTE IMMEDIATE 'SELECT 1'")


@pytest.mark.parametrize("stmt", [
    "CREATE VIEW core.v_q AS WITH t AS (SELECT * FROM query_table('team' || '_b.ingest_log')) SELECT * FROM t",
    "CREATE VIEW core.v_q AS SELECT * FROM query_table(concat('team', '_b', '.ingest_log'))",
    "CREATE VIEW core.v_q AS WITH query AS (SELECT 1 AS a) SELECT * FROM query(  'SELECT 1')",
])
def test_string_built_identifiers_inside_table_functions_blocked(stmt):
    """ADVERSARY: identifiers built with concat/|| inside query_table, also inside a CTE, are blocked
    (baseline for the allowlist)."""
    assert statement_problems(stmt, "core.v_q")


@pytest.mark.parametrize("body", [
    "SELECT status, COUNT(*) AS n FROM core.claim GROUP BY status",
    "WITH t AS (SELECT * FROM core.claim) SELECT * FROM t",
    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) SELECT n FROM r",
    "SELECT claim_id, CAST(amount AS DECIMAL(18,2)) AS amt FROM core.claim",
    "SELECT claim_id, SUM(amount) OVER (PARTITION BY status ORDER BY claim_id) AS run FROM core.claim",
    "SELECT COUNT(*) FILTER (WHERE status = 'open') AS n_open FROM core.claim",
    "SELECT * FROM core.claim c WHERE NOT EXISTS (SELECT 1 FROM core.claim d WHERE d.claim_id = c.claim_id + 1)",
    "SELECT year(reported_date) AS y, month(reported_date) AS m FROM core.claim",
    "SELECT regexp_replace(status, 'o', '0') AS s FROM core.claim",
    "SELECT string_agg(status, ',') AS s FROM core.claim",
    "SELECT strftime(reported_date, '%Y-%m') AS ym FROM core.claim",
    "SELECT date_add(reported_date, INTERVAL 1 DAY) AS d FROM core.claim",
    "SELECT CASE WHEN amount > 50 THEN 'big' ELSE 'small' END AS size FROM core.claim",
    "SELECT reported_date::DATE AS d, amount::DOUBLE AS a FROM core.claim",
    "SELECT * FROM core.claim WHERE status IN ('open', 'reopened')",
    "SELECT * FROM core.claim WHERE note_of('x') IS NULL" if False else "SELECT ROUND(AVG(amount), 2) AS a FROM core.claim",
])
def test_no_false_block_on_legitimate_view_ddl(body):
    """ADVERSARY: ordinary reporting-view DDL (CTEs, windows, FILTER, casts, common date/string functions) must
    not be blocked. Every statement here runs on DuckDB."""
    stmt = f"CREATE OR REPLACE VIEW core.v_ok AS {body}"
    _duck().execute(stmt)
    assert statement_problems(stmt, "core.v_ok") == []
