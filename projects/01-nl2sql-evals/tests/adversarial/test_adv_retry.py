"""ADVERSARY: can the infra-only retry rule hide real model errors?"""

from nl2sql import pipeline
from nl2sql.providers.base import Generation
from nl2sql.providers.claude_cli import ClaudeCliProvider

from ._helpers import FakeRunner, envelope_without_usage


class Scripted:
    name, model, tier = "claude-cli", "claude-haiku-5-5", "haiku"

    def __init__(self, gens):
        self.gens, self.calls = list(gens), 0

    def generate(self, q, card):
        self.calls += 1
        return self.gens.pop(0)


def test_unparseable_answer_without_usage_block_is_not_retried(con):
    """ADVERSARY: README says an unparseable answer is never retried, but 'infra' is decided by token
    count only. An envelope without a usage block makes a model's non-JSON answer look like 0 tokens,
    so it is retried and the model failure is replaced by a second attempt."""
    runner = FakeRunner(envelope_without_usage("I think the answer is 42"))
    res = pipeline.ask("q", ClaudeCliProvider(runner=runner), con)
    assert runner.calls == 1 and res.retries == 0


def test_cli_reported_model_error_without_tokens_is_not_retried(con):
    """ADVERSARY: an is_error envelope (API 400, model not supported) with no usage is a
    configuration/model error, not a transport failure, yet it qualifies as infra."""
    runner = FakeRunner(envelope_without_usage("API Error: 400 model not supported", is_error=True))
    pipeline.ask("q", ClaudeCliProvider(runner=runner), con)
    assert runner.calls == 1


def test_execution_error_from_model_sql_is_never_retried(con):
    """ADVERSARY: positive control - SQL that fails in DuckDB is a model error and is not retried."""
    p = Scripted([Generation("sql", sql="SELECT no_such_col FROM policies", input_tokens=1, output_tokens=1),
                  Generation("sql", sql="SELECT COUNT(*) FROM policies", input_tokens=1, output_tokens=1)])
    res = pipeline.ask("q", p, con)
    assert res.action == "error" and p.calls == 1 and res.retries == 0


def test_retry_is_visible_in_the_record(con):
    """ADVERSARY: positive control - a retry leaves first_error and retries=1 in the record."""
    p = Scripted([Generation("error", message="claude CLI failed: timed out"),
                  Generation("sql", sql="SELECT COUNT(*) FROM policies", input_tokens=1, output_tokens=1)])
    res = pipeline.ask("q", p, con)
    assert res.retries == 1 and "timed out" in res.first_error
