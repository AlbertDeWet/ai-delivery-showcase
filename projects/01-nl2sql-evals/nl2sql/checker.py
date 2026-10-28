"""Adversarial checker: re-derive each answer's key number with an independent query.

The key number of an answer is its single value, or the sum of its last numeric
column (a control total). The checker asks the provider, through a separate
prompt/code path, for one query returning the grand total of the measure with the
same filters, runs it through the same guard, and flags any disagreement.

Abstains on ratios, averages, top-N and text answers, where a grand total does
not reconcile. The checker never has access to the gold SQL.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from nl2sql import executor, guard
from nl2sql.pipeline import AskResult, call_with_infra_retry
from nl2sql.schema import SCHEMA_CARD


@dataclass
class CheckResult:
    status: str  # agree | disagree | abstain
    key_number: float | None = None
    control: float | None = None
    note: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


def key_number(columns, rows) -> float | None:
    if not rows:
        return None
    numeric_cols = [i for i in range(len(columns))
                    if all(r[i] is None or isinstance(r[i], (int, float)) and not isinstance(r[i], bool)
                           for r in rows) and any(r[i] is not None for r in rows)]
    if not numeric_cols:
        return None
    i = numeric_cols[-1]
    return float(sum(r[i] for r in rows if r[i] is not None))


def check(res: AskResult, provider, con, schema_card: str = SCHEMA_CARD, retry_infra: bool = True) -> CheckResult:
    if res.action != "sql":
        return CheckResult("abstain", note="no answer to check")
    k = key_number(res.columns, res.rows)
    if k is None:
        return CheckResult("abstain", note="answer has no numeric column")
    try:
        gen, _, _ = call_with_infra_retry(provider.control_total, res.question, schema_card,
                                          retry_infra=retry_infra)
    except ValueError as exc:
        return CheckResult("abstain", key_number=k, note=str(exc))
    out = CheckResult("abstain", key_number=k, input_tokens=gen.input_tokens, output_tokens=gen.output_tokens)
    if gen.action != "sql":
        out.note = gen.message or "checker declined"
        return out
    g = guard.check(gen.sql)
    if not g.ok:
        out.note = "control query blocked: " + "; ".join(g.reasons)
        return out
    try:
        _, rows = executor.run(g.sql, con)
    except Exception as exc:
        out.note = f"control query failed: {exc}"
        return out
    if len(rows) != 1 or len(rows[0]) < 1 or not isinstance(rows[0][0], (int, float)):
        out.note = "control query did not return one number"
        return out
    out.control = float(rows[0][0])
    same = math.isclose(out.control, k, rel_tol=1e-3, abs_tol=1e-6)
    out.status = "agree" if same else "disagree"
    return out
