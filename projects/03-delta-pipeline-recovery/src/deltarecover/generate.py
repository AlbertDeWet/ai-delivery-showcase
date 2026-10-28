"""Deterministic synthetic GL generator for Contoso Insurance (fictional sample data).

Everything here is invented. Same seed -> byte-identical output.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

REPORTING_CCY = "USD"
CENT = Decimal("0.01")
RATE_Q = Decimal("0.000001")

# Fictional entities, each with a functional currency.
ENTITIES = {
    "CI-E01": "USD", "CI-E02": "EUR", "CI-E03": "GBP", "CI-E04": "JPY",
    "CI-E05": "CHF", "CI-E06": "CAD", "CI-E07": "AUD", "CI-E08": "SEK",
}
# Indicative USD value of 1 unit of currency (invented base levels, not market data).
BASE_RATE = {
    "USD": Decimal("1"), "EUR": Decimal("1.08"), "GBP": Decimal("1.27"), "JPY": Decimal("0.0068"),
    "CHF": Decimal("1.12"), "CAD": Decimal("0.74"), "AUD": Decimal("0.66"), "SEK": Decimal("0.095"),
}
ALLOWED_CCY = frozenset(BASE_RATE)


def months(year: int = 2025, n: int = 12) -> list[str]:
    return [f"{year}-{m:02d}" for m in range(1, n + 1)]


@dataclass
class Dataset:
    transactions: list[dict] = field(default_factory=list)   # raw source rows
    fx: list[dict] = field(default_factory=list)             # currency, month, rate_to_usd
    accounts: list[str] = field(default_factory=list)
    months: list[str] = field(default_factory=list)


def generate(seed: int = 20250101, n_accounts: int = 75, n_months: int = 12,
             density: float = 0.7, max_txn: int = 8) -> Dataset:
    rng = random.Random(seed)
    ms = months(n=n_months)
    accounts = [f"{4000 + i * 10}" for i in range(n_accounts)]
    ds = Dataset(accounts=accounts, months=ms)

    for ccy, base in BASE_RATE.items():
        for i, m in enumerate(ms):
            if ccy == REPORTING_CCY:
                rate = Decimal("1")
            else:
                drift = Decimal(1) + Decimal(rng.randint(-300, 300)) / Decimal(10000)
                rate = (base * drift).quantize(RATE_Q, ROUND_HALF_UP)
            ds.fx.append({"currency": ccy, "month": m, "rate_to_usd": rate})

    seq = 0
    for ent, ccy in ENTITIES.items():
        scale = 150 if ccy == "JPY" else 1
        for m in ms:
            for acct in accounts:
                if rng.random() > density:
                    continue
                for _ in range(rng.randint(1, max_txn)):
                    seq += 1
                    cents = rng.randint(-500_000_00, 2_000_000_00) * scale
                    ds.transactions.append({
                        "txn_id": f"T{seq:08d}",
                        "entity": ent,
                        "account": acct,
                        "month": m,
                        "currency": ccy,
                        "amount_local": (Decimal(cents) / 100).quantize(CENT),
                    })
    return ds


def to_reporting(amount_local: Decimal, rate: Decimal) -> Decimal:
    return (amount_local * rate).quantize(CENT, ROUND_HALF_UP)
