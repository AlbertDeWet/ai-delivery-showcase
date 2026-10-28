from envalign.normalise import env_mask, env_substitute, norm_job, norm_sql, row_hash


def test_cosmetic_sql_differences_are_equal():
    a = "CREATE VIEW core.v AS SELECT a, b FROM core.t WHERE s = 'Open';"
    b = "create or replace view core.v as\n  -- comment\n  select a,b   from CORE.T /* x */ where s='Open'"
    assert norm_sql(a) == norm_sql(b)


def test_literal_case_is_preserved():
    assert norm_sql("SELECT * FROM t WHERE s = 'Open'") != norm_sql("SELECT * FROM t WHERE s = 'open'")


def test_comment_markers_inside_literals_are_kept():
    assert "'--x'" in norm_sql("SELECT '--x' AS c")


def test_env_mask_and_substitute():
    assert env_mask("lake://contoso_dev/landing") == env_mask("lake://contoso_prod/landing")
    assert env_substitute("/bundles/x/dev/src", "dev", "uat") == "/bundles/x/uat/src"
    assert env_mask("developer") == "developer"


def test_job_expected_fields_masked_but_real_fields_not():
    base = {"name": "j", "job_id": 1, "schedule": {"quartz_cron_expression": "0 0 1 * * ?", "pause_status": "PAUSED"},
            "tasks": [{"task_key": "b"}, {"task_key": "a"}]}
    other = {"name": "j", "job_id": 2, "schedule": {"quartz_cron_expression": "0 0 1 * * ?", "pause_status": "UNPAUSED"},
             "tasks": [{"task_key": "a"}, {"task_key": "b"}]}
    assert norm_job(base) != norm_job(other)  # pause status differs
    assert norm_job(base, "expected") == norm_job(other, "expected")
    other["schedule"]["quartz_cron_expression"] = "0 0 2 * * ?"
    assert norm_job(base, "expected") != norm_job(other, "expected")


def test_row_hash_env_masking():
    a, b = {"key": "k", "value": "contoso_dev"}, {"key": "k", "value": "contoso_uat"}
    assert row_hash(a) != row_hash(b)
    assert row_hash(a, mask_env=True) == row_hash(b, mask_env=True)
