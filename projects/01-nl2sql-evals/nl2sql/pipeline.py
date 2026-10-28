"""question -> schema card -> generate -> guard -> execute -> answer."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from nl2sql import executor, guard
from nl2sql.providers.base import INFRA_PREFIX, Generation, Provider
from nl2sql.schema import SCHEMA_CARD


@dataclass
class AskResult:
    question: str
    action: str  # sql | refuse | clarify | blocked | error
    sql: str = ""
    guarded_sql: str = ""
    guard_reasons: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    retries: int = 0
    first_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rows"] = [list(map(_jsonable, r)) for r in self.rows[:50]]
        return d


def _jsonable(v):
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def format_answer(columns: list[str], rows: list[tuple], max_rows: int = 20) -> str:
    """Deterministic answer text. Data values are never sent back to a model, so text stored
    in the data (for example an injected instruction in a claim description) is only displayed."""
    if not rows:
        return "No rows matched."
    if len(rows) == 1 and len(columns) == 1:
        return f"{columns[0]} = {rows[0][0]}"
    head = "| " + " | ".join(columns) + " |\n|" + "---|" * len(columns) + "\n"
    body = "\n".join("| " + " | ".join(str(v) for v in r) + " |" for r in rows[:max_rows])
    more = f"\n... {len(rows) - max_rows} more rows" if len(rows) > max_rows else ""
    return head + body + more


MAX_INFRA_RETRIES = 1


def is_infra_error(gen: Generation) -> bool:
    """Infrastructure failure: an explicit transport failure (CLI timeout, or the process could not
    start), marked by the provider with infra=True / the INFRA_PREFIX message, with no tokens consumed.
    "0 tokens" alone is NOT enough: a missing usage block, a CLI-reported API error (is_error), a
    non-zero exit, or an unparseable answer are outcomes, never retried and never shown as "not run"."""
    if gen.action != "error" or gen.input_tokens or gen.output_tokens:
        return False
    return bool(getattr(gen, "infra", False)) or (gen.message or "").startswith(INFRA_PREFIX)


NO_OUTPUT_PREFIXES = (INFRA_PREFIX, "CLI reported error:", "claude CLI exit")


def is_no_output_record(rec: dict) -> bool:
    """A stored record where the model produced no output at all: zero tokens, and a transport
    failure, a CLI-reported API error (for example an unsupported model) or a failed process.
    Used only to decide whether a whole run is 'not run'. Execution errors and unparseable
    answers are model output and always count as failures."""
    return (rec.get("action") == "error" and not rec.get("input_tokens") and not rec.get("output_tokens")
            and (rec.get("error") or "").startswith(NO_OUTPUT_PREFIXES))


def is_infra_record(rec: dict) -> bool:
    """Same rule applied to a stored eval record."""
    return (rec.get("action") == "error" and not rec.get("input_tokens") and not rec.get("output_tokens")
            and (rec.get("error") or "").startswith(INFRA_PREFIX))


def call_with_infra_retry(fn, *args, retry_infra: bool = True) -> tuple[Generation, int, str]:
    """Call fn(*args); retry at most MAX_INFRA_RETRIES times, only on infrastructure errors.
    Same rule for every provider and tier. Returns (generation, retries used, first error)."""
    gen = fn(*args)
    retries, first_error = 0, ""
    while retry_infra and is_infra_error(gen) and retries < MAX_INFRA_RETRIES:
        first_error = first_error or gen.message
        retries += 1
        gen = fn(*args)
    return gen, retries, first_error


def ask(question: str, provider: Provider, con, schema_card: str = SCHEMA_CARD,
        retry_infra: bool = True) -> AskResult:
    t0 = time.perf_counter()
    gen, retries, first_error = call_with_infra_retry(provider.generate, question, schema_card,
                                                      retry_infra=retry_infra)
    res = AskResult(question=question, action=gen.action, sql=gen.sql, input_tokens=gen.input_tokens,
                    output_tokens=gen.output_tokens, cost_usd=gen.cost_usd, retries=retries,
                    first_error=first_error)
    if gen.action in ("refuse", "clarify"):
        res.answer = gen.message
    elif gen.action == "error":
        res.error = gen.message
    else:
        g = guard.check(gen.sql)
        if not g.ok:
            res.action = "blocked"
            res.guard_reasons = g.reasons
            res.answer = "Blocked by the SQL guard: " + "; ".join(g.reasons)
        else:
            res.guarded_sql = g.sql
            try:
                res.columns, res.rows = executor.run(g.sql, con)
                res.answer = format_answer(res.columns, res.rows)
            except Exception as exc:  # execution error is a wrong answer, not a crash
                res.action = "error"
                res.error = f"execution error: {exc}"
    res.latency_s = time.perf_counter() - t0
    return res
