"""Independent control rounding (round 1 fix F05).

Deliberately a separate implementation from generate.to_reporting: integer arithmetic on scaled
Decimal tuples, rounding half away from zero (Decimal ROUND_HALF_UP semantics). A bug in the
pipeline's rounding therefore shows up as reconciliation breaks instead of cancelling out.
"""
from __future__ import annotations

from decimal import Decimal


def control_round_usd(amount_local: Decimal, rate: Decimal) -> Decimal:
    a, r = Decimal(amount_local), Decimal(rate)
    # exact product as integer of 10^-k units
    k = -(a.as_tuple().exponent + r.as_tuple().exponent)
    units = int(a.scaleb(-a.as_tuple().exponent)) * int(r.scaleb(-r.as_tuple().exponent))
    if k <= 2:
        return Decimal(units).scaleb(-k).quantize(Decimal("0.01"))
    div = 10 ** (k - 2)
    q, rem = divmod(abs(units), div)
    if rem * 2 >= div:
        q += 1
    return Decimal(-q if units < 0 else q).scaleb(-2)
