"""The claude-cli provider, tested against a mocked subprocess (no real CLI calls)."""

import json
import subprocess

import pytest

from nl2sql import evals, pipeline
from nl2sql.providers.claude_cli import ClaudeCliProvider, build_command, parse_envelope, parse_model_json


def envelope(text, cost=0.0123, is_error=False, in_tok=1500, out_tok=40):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": is_error, "duration_ms": 2100,
        "duration_api_ms": 1800, "result": text, "total_cost_usd": cost,
        "usage": {"input_tokens": in_tok, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                  "output_tokens": out_tok},
    })


class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr="", exc=None):
        self.stdout, self.returncode, self.stderr, self.exc = stdout, returncode, stderr, exc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def test_command_disables_tools_and_uses_json():
    cmd = build_command("claude", "claude-haiku-5-5", "SYS")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-5-5"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--tools") + 1] == ""
    for flag in ("--strict-mcp-config", "--safe-mode", "--no-session-persistence"):
        assert flag in cmd


@pytest.mark.parametrize("tier,model", [("haiku", "claude-haiku-5-5"), ("sonnet", "claude-sonnet-5-5"),
                                        ("opus", "claude-opus-5-5")])
def test_tier_model_ids(tier, model):
    assert ClaudeCliProvider(tier=tier, runner=FakeRunner()).model == model


def test_unknown_tier_rejected():
    with pytest.raises(ValueError):
        ClaudeCliProvider(tier="giant")


def test_prompt_goes_via_stdin_and_parses_sql():
    run = FakeRunner(envelope('{"action": "sql", "sql": "SELECT COUNT(*) FROM policies"}'))
    g = ClaudeCliProvider(tier="sonnet", runner=run).generate("How many policies?", "CARD")
    assert g.action == "sql" and g.sql == "SELECT COUNT(*) FROM policies"
    assert g.input_tokens == 1500 and g.output_tokens == 40 and g.cost_usd == pytest.approx(0.0123)
    cmd, kw = run.calls[0]
    assert "How many policies?" in kw["input"]
    assert "How many policies?" not in " ".join(cmd)
    assert "CARD" in cmd[cmd.index("--system-prompt") + 1]
    assert not any(k.startswith("CLAUDECODE") for k in kw["env"])


def test_fenced_json_is_parsed():
    assert parse_model_json('```json\n{"action": "refuse", "message": "no"}\n```')["action"] == "refuse"


def test_refusal_passthrough():
    run = FakeRunner(envelope('{"action": "refuse", "message": "Read-only assistant."}'))
    g = ClaudeCliProvider(runner=run).generate("Drop the policies table", "CARD")
    assert g.action == "refuse" and "Read-only" in g.message


def test_unparseable_output_is_error():
    g = ClaudeCliProvider(runner=FakeRunner(envelope("I think the answer is 42"))).generate("q", "c")
    assert g.action == "error"


def test_cli_error_envelope():
    g = ClaudeCliProvider(runner=FakeRunner(envelope("rate limited", is_error=True))).generate("q", "c")
    assert g.action == "error" and "rate limited" in g.message


def test_nonzero_exit_without_stdout():
    g = ClaudeCliProvider(runner=FakeRunner("", returncode=1, stderr="not logged in")).generate("q", "c")
    assert g.action == "error" and "not logged in" in g.message


def test_timeout_is_error():
    run = FakeRunner(exc=subprocess.TimeoutExpired("claude", 1))
    assert ClaudeCliProvider(runner=run).generate("q", "c").action == "error"


def test_envelope_sums_cache_tokens():
    e = parse_envelope(json.dumps({"result": "x", "usage": {"input_tokens": 3, "cache_read_input_tokens": 100,
                                                             "cache_creation_input_tokens": 7,
                                                             "output_tokens": 5}}))
    assert e["input_tokens"] == 110 and e["output_tokens"] == 5 and e["cost_usd"] is None


def test_guard_still_applies_to_model_sql(con):
    run = FakeRunner(envelope('{"action": "sql", "sql": "SELECT 1; DROP TABLE claims"}'))
    res = pipeline.ask("q", ClaudeCliProvider(runner=run), con)
    assert res.action == "blocked"


def test_eval_with_mocked_cli_records_tokens_and_cost(db_path):
    run = FakeRunner(envelope('{"action": "sql", "sql": "SELECT COUNT(*) FROM policies"}'))
    cases = [c for c in evals.load_cases() if c["id"] in ("s01", "a02")]
    out = evals.run_eval(ClaudeCliProvider(tier="haiku", runner=run), repeats=2, cases=cases, db_path=db_path)
    s = out["summary"]
    assert out["model"] == "claude-haiku-5-5" and s["n_records"] == 4
    assert s["by_category"]["simple"]["correct"] == 2
    assert s["by_category"]["adversarial_destructive"]["correct"] == 0  # answered instead of refusing
    assert s["tokens_in"] > 0 and s["api_equivalent_cost_usd"] > 0
    assert s["pass^2_pct"] == 50.0


# ---- malformed output must always become an error Generation (review item 2) ----

MALFORMED_STDOUT = {
    "null_envelope": "null",
    "list_envelope": json.dumps([{"type": "result", "result": '{"action": "sql", "sql": "SELECT 1"}'}]),
    "string_envelope": json.dumps("just a string"),
    "number_envelope": "42",
    "not_json": "this is not json",
    "empty": "",
    "result_is_number": json.dumps({"result": 123, "usage": {}}),
    "result_is_dict": json.dumps({"result": {"action": "sql"}, "usage": {}}),
    "usage_is_list": json.dumps({"result": '{"action": "sql", "sql": "SELECT 1"}', "usage": [1, 2]}),
    "usage_is_string": json.dumps({"result": '{"action": "sql", "sql": "SELECT 1"}', "usage": "lots"}),
    "token_count_is_string": json.dumps({"result": '{"action": "sql", "sql": "SELECT 1"}',
                                         "usage": {"input_tokens": "many"}}),
    "cost_is_string": json.dumps({"result": '{"action": "sql", "sql": "SELECT 1"}', "total_cost_usd": "free"}),
    "sql_is_list": envelope('{"action": "sql", "sql": ["SELECT 1"]}'),
    "sql_is_number": envelope('{"action": "sql", "sql": 1}'),
    "message_is_dict": envelope('{"action": "refuse", "message": {"why": "x"}}'),
    "action_is_list": envelope('{"action": ["sql"], "sql": "SELECT 1"}'),
    "model_json_is_list": envelope('[{"action": "sql", "sql": "SELECT 1"}]'),
    "model_json_invalid": envelope('{"action": "sql", "sql": }'),
    "sql_action_without_sql": envelope('{"action": "sql"}'),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_STDOUT))
def test_malformed_output_is_error(name):
    g = ClaudeCliProvider(runner=FakeRunner(MALFORMED_STDOUT[name])).generate("q", "c")
    assert g.action == "error", (name, g)


def test_non_string_stdout_is_error():
    g = ClaudeCliProvider(runner=FakeRunner(stdout=None, returncode=0)).generate("q", "c")
    assert g.action == "error"


def test_decode_errors_replaced_not_raised():
    run = FakeRunner(envelope('{"action": "refuse", "message": "ok"}'))
    ClaudeCliProvider(runner=run).generate("q", "c")
    assert run.calls[0][1]["errors"] == "replace"
    assert run.calls[0][1]["encoding"] == "utf-8"


def test_unicode_error_from_runner_is_error():
    run = FakeRunner(exc=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte"))
    assert ClaudeCliProvider(runner=run).generate("q", "c").action == "error"


# ---- child environment: subscription route only (review item 4) ----

def test_child_env_strips_keys_and_claude_vars():
    from nl2sql.providers.claude_cli import child_env

    env = child_env({
        "ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
        "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_ENTRYPOINT": "x", "CLAUDECODE": "1",
        "PATH": "/bin", "USERPROFILE": "home", "ANTHROPIC_MODEL_NOTE": "kept",
    })
    assert env == {"PATH": "/bin", "USERPROFILE": "home", "ANTHROPIC_MODEL_NOTE": "kept"}


def test_runner_receives_stripped_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://elsewhere")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("CLAUDECODE", "1")
    run = FakeRunner(envelope('{"action": "refuse", "message": "ok"}'))
    ClaudeCliProvider(runner=run).generate("q", "c")
    passed = run.calls[0][1]["env"]
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_VERTEX",
              "CLAUDECODE"):
        assert k not in passed
