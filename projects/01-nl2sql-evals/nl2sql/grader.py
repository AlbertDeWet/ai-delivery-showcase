"""Execution-based grading: compare result sets, never SQL strings.

Grader v2 (adversarial round 1). Rules:
  * order-insensitive (multiset comparison of rows); row counts must match exactly;
  * integral gold (counts, years, whole amounts): the prediction must be integral-valued and
    exactly equal. 1000 vs 1000.4 fails;
  * non-integral gold: the prediction must agree at the gold's precision. With d = the number of
    decimals in the gold value, capped at 4, the tolerance is half a unit in the last place:
    0.5 * 10**-max(d, floor), where floor is 2 for |gold| >= 1 (money to the cent) and 4 for
    |gold| < 1 (ratios). So 2-dp money gets an absolute 0.005, and 4-dp ratios get 0.00005.
    There is no relative tolerance, so dropping one 82.88 premium from a 7.4M total fails;
  * NaN equals only NaN, never NULL;
  * column names are ignored, except when the prediction is wider than the gold;
  * extra predicted columns:
      - one-row results: the gold columns must line up with the prediction's first or last
        columns, positionally, or by name when names are given. No search over "any column",
        so a wide row of candidate numbers cannot hedge;
      - multi-row results: gold columns may match any subset of predicted columns, since every
        value in a column has to match;
  * same width: any column order is accepted (up to 4 columns).
"""

from __future__ import annotations

import datetime as _dt
import decimal
import math
from itertools import permutations

GRADER_VERSION = "v2-round2"
MAX_DECIMALS = 4
INTEGRAL_NOISE = 1e-6
_NAN = "\x00NaN"  # sentinel so NaN never collapses into NULL


def _norm(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float, decimal.Decimal)):
        f = float(v)
        return _NAN if math.isnan(f) else f
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return str(v).strip()


def _key(v):
    if v is None:
        return (0, 0.0, "")
    if v == _NAN:
        return (1, 0.0, "nan")
    if isinstance(v, float):
        return (2, round(v, 2), "")
    return (3, 0.0, v)


def _decimals(g: float) -> int:
    for d in range(0, MAX_DECIMALS + 1):
        scaled = g * 10 ** d
        if math.isclose(scaled, round(scaled), rel_tol=1e-12, abs_tol=1e-6):
            return d
    return MAX_DECIMALS


def numbers_equal(gold: float, pred: float) -> bool:
    if math.isinf(gold) or math.isinf(pred):
        return gold == pred
    if gold.is_integer():
        # Exact up to float summation noise (R7): 1000.0000000001 passes, 1000.4 does not.
        return abs(pred - gold) <= INTEGRAL_NOISE
    floor = 2 if abs(gold) >= 1 else 4
    tol = 0.5 * 10 ** -max(_decimals(gold), floor) + 1e-9
    return abs(gold - pred) <= tol


def values_equal(gold, pred) -> bool:
    """Compare one gold value with one predicted value (argument order matters)."""
    if gold is None or pred is None:
        return gold is None and pred is None
    if gold == _NAN or pred == _NAN:
        return gold == pred
    if isinstance(gold, float) and isinstance(pred, float):
        return numbers_equal(gold, pred)
    if isinstance(gold, float) != isinstance(pred, float):
        # numeric string vs number, e.g. '2024' vs 2024.0
        try:
            fg, fp = float(gold), float(pred)
        except (TypeError, ValueError):
            return False
        if math.isnan(fg) or math.isnan(fp):
            return False
        return numbers_equal(fg, fp)
    return gold == pred


def _sorted_rows(rows):
    return sorted(rows, key=lambda r: tuple(_key(v) for v in r))


def _rows_match(gold, pred) -> bool:
    if len(gold) != len(pred):
        return False
    g_sorted, p_sorted = _sorted_rows(gold), _sorted_rows(pred)
    if all(all(values_equal(a, b) for a, b in zip(gr, pr)) for gr, pr in zip(g_sorted, p_sorted)):
        return True
    # Fallback for near-ties where tolerance changes sort order: greedy matching.
    remaining = list(pred)
    for gr in gold:
        for i, pr in enumerate(remaining):
            if all(values_equal(a, b) for a, b in zip(gr, pr)):
                del remaining[i]
                break
        else:
            return False
    return True


def _project(rows, idx):
    return [tuple(r[i] for i in idx) for r in rows]


HEDGE_BAND = 0.10


def _is_hedge(gold, pred, idx) -> bool:
    """R6: an extra predicted column that is numeric and lies within HEDGE_BAND of a matched numeric
    gold column on every row is a second candidate answer (for example 1205 next to 1206), not
    context. Label columns, and context of a different magnitude (incurred and GWP next to a loss
    ratio), are not hedges. Rows are compared after sorting on the matched columns."""
    extra = [j for j in range(len(pred[0])) if j not in idx]
    if not extra:
        return False
    key = lambda r: tuple(_key(v) for v in r)
    g_sorted = sorted(gold, key=key)
    p_sorted = sorted(pred, key=lambda r: key(tuple(r[i] for i in idx)))
    for gi in range(len(gold[0])):
        gvals = [r[gi] for r in g_sorted]
        if not all(isinstance(v, float) for v in gvals):
            continue
        for j in extra:
            pvals = [r[j] for r in p_sorted]
            if all(isinstance(v, float) for v in pvals) and all(
                    abs(p - g) <= HEDGE_BAND * max(abs(g), 1e-9) for p, g in zip(pvals, gvals)):
                return True
    return False


def result_sets_match(gold_rows, pred_rows, gold_cols=None, pred_cols=None) -> bool:
    gold = [tuple(_norm(v) for v in r) for r in gold_rows]
    pred = [tuple(_norm(v) for v in r) for r in pred_rows]
    if len(gold) != len(pred):
        return False
    if not gold:
        return True
    gw, pw = len(gold[0]), len(pred[0])
    if pw < gw:
        return False
    if pw == gw:
        if _rows_match(gold, pred):
            return True
        if gw <= 4:
            return any(_rows_match(gold, _project(pred, perm)) for perm in permutations(range(pw)))
        return False

    # Wider prediction.
    #  * one row (R3): the gold columns must line up with the prediction's first or last columns,
    #    or match by column name;
    #  * many rows (R5): a bounded subset search is kept, because correct per-group answers often
    #    carry context columns between the label and the measure (j01: id, name, line, count;
    #    m02: line, GWP, incurred, ratio), and the literal positional rule grades those wrong.
    #  * either way (R5, R6): a match is rejected when an extra numeric column is a second
    #    candidate for the measure (within HEDGE_BAND of it on every row).
    candidates = [tuple(range(gw)), tuple(range(pw - gw, pw))]  # left- or right-aligned
    if gold_cols and pred_cols and len(gold_cols) == gw and len(pred_cols) == pw:
        lower = [str(c).lower() for c in pred_cols]
        try:
            candidates.append(tuple(lower.index(str(c).lower()) for c in gold_cols))
        except ValueError:
            pass
    if len(gold) > 1 and pw <= 8:
        candidates += [perm for perm in permutations(range(pw), gw) if perm not in candidates]
    return any(_rows_match(gold, _project(pred, idx)) and not _is_hedge(gold, pred, idx)
               for idx in candidates)
