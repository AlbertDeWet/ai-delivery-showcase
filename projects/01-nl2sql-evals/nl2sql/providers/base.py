"""Provider interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Generation:
    action: str  # "sql" | "refuse" | "clarify" | "error"
    sql: str = ""
    message: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # only when the backend reports it
    latency_s: float = 0.0
    raw: dict = field(default_factory=dict)
    # True only for an explicit transport failure (CLI timeout, process could not start).
    infra: bool = False


# Error messages that start with this prefix are infrastructure failures (no model outcome).
INFRA_PREFIX = "claude CLI failed:"


class Provider(Protocol):
    name: str
    model: str

    def generate(self, question: str, schema_card: str) -> Generation: ...

    def control_total(self, question: str, schema_card: str) -> Generation:
        """Independent single-number query used by the adversarial checker."""
        ...
