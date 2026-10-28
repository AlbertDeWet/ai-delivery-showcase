"""Incident A: duplicate bug detector."""
from __future__ import annotations

from . import recon


def doubled_entity_months(gold, summary) -> dict:
    """An entity-month is 'doubled' when gold has exactly 2x the control row count AND 2x the amount.
    Any other mismatch is reported separately as 'other_mismatch' so it is never hidden."""
    g, s = recon.entity_month(gold), recon.summary_entity_month(summary)
    doubled, other = [], []
    for key in sorted(set(g) | set(s)):
        gn, ga = g.get(key, (0, 0))
        sn, sa = s.get(key, (0, 0))
        if (gn, ga) == (sn, sa):
            continue
        if sn and gn == 2 * sn and ga == 2 * sa:
            doubled.append({"entity": key[0], "month": key[1]})
        else:
            other.append({"entity": key[0], "month": key[1], "gold_rows": gn, "summary_rows": sn})
    dup_ids = recon._q("SELECT count(*) FROM (SELECT upper(trim(txn_id)) FROM f GROUP BY 1 HAVING count(*) > 1)",
                       f=gold)[0][0]
    return {"entity_months_checked": len(set(g) | set(s)), "doubled": doubled,
            "doubled_count": len(doubled), "other_mismatch": other, "duplicate_txn_ids": dup_ids}
