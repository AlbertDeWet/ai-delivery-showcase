from envalign.demo import demo_exit_code
from envalign.sqlguard import statement_problems


def test_plain_generated_statements_pass():
    ok = ["CREATE OR REPLACE VIEW core.v AS SELECT avg(CASE WHEN ((status = 'x')) THEN (1.0) ELSE 0.0 END) AS r "
          "FROM core.\"policy\" WHERE x IN (1, 2) AND NOT EXISTS (SELECT 1 FROM core.claim)",
          "CREATE TABLE core.v(key VARCHAR, value DECIMAL(10,2))", "INSERT INTO core.v VALUES ('a', NULL)",
          "DELETE FROM core.v WHERE key = 'a'", "DROP TABLE IF EXISTS core.v",
          "CREATE TABLE core.v AS SELECT * FROM _backup.v__x"]
    for s in ok:
        assert statement_problems(s, "core.v") == [], s


def test_spelling_tricks_are_blocked():
    for s in ["EXECUTE/**/IMMEDIATE 'x'", "CREATE VIEW core.v AS SELECT * FROM \"query\"('x')",
              "CREATE VIEW core.v AS SELECT * FROM query/**/('x')", "CREATE VIEW core.v AS SELECT * FROM range(3)",
              "CREATE VIEW core.v AS SELECT json_execute_serialized_sql('{}')", "SET schema = 'x'",
              "CREATE VIEW core.v AS SELECT 1; DROP TABLE core.claim", "DELETE FROM core.other"]:
        assert statement_problems(s, "core.v"), s


def test_demo_exit_code():
    ok_steps = {"a (risks not accepted)": [{"status": "refused", "lint": ["R1"]}], "b": [{"status": "applied"}]}
    assert demo_exit_code(ok_steps, {"status": "rolled_back", "restore_verified": True}) == 0
    assert demo_exit_code({"b": [{"status": "ROLLBACK_PARTIAL"}]}, {"status": "rolled_back"}) == 3
    assert demo_exit_code({"b": [{"status": "refused", "lint": []}]}, {"status": "rolled_back",
                                                                       "restore_verified": True}) == 1


def test_ast_guard_round4_cases():
    blocked = ["CREATE VIEW core.v AS SELECT $$'$$ AS a",
               "CREATE VIEW core.v AS SELECT * /* /* */ ' */ FROM core.claim",
               "CREATE VIEW core.v AS SELECT * FROM '/tmp/ledger.jsonl'",
               "CREATE VIEW core.v AS SELECT * FROM glob('*')",
               "CREATE VIEW core.v AS SELECT * FROM team_b.ingest_log",
               "CREATE VIEW core.v AS SELECT * FROM other_cat.core.claim",
               "UPDATE core.v SET a = 1"]
    for s in blocked:
        assert statement_problems(s, "core.v"), s
    allowed = ["CREATE VIEW core.v AS WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 3) "
               "SELECT n FROM r",
               "CREATE VIEW core.v AS SELECT year(d), regexp_replace(s, 'o', '0'), string_agg(s, ','), "
               "date_add(d, INTERVAL 1 DAY) FROM core.claim GROUP BY ALL"]
    for s in allowed:
        assert statement_problems(s, "core.v") == [], (s, statement_problems(s, "core.v"))
