"""ADVERSARY helpers: build tiny TMDL models and PBIR-style reports in a tmp folder."""
from __future__ import annotations

import json
from pathlib import Path

FACT = """table FactClaims
\tmeasure 'Total Paid' = SUM(FactClaims[PaidAmount])

\tcolumn PolicyKey
\t\tdataType: int64
\t\tsourceColumn: PolicyKey

\tcolumn DateKey
\t\tdataType: int64
\t\tsourceColumn: DateKey

\tcolumn PaidAmount
\t\tdataType: double
\t\tsourceColumn: PaidAmount

\tcolumn ReserveAmount
\t\tdataType: double
\t\tsourceColumn: ReserveAmount

\tcolumn ClaimStatus
\t\tdataType: string
\t\tsourceColumn: ClaimStatus
"""

DIMPOLICY = """table DimPolicy
\tcolumn PolicyKey
\t\tdataType: int64
\t\tsourceColumn: PolicyKey

\tcolumn LineOfBusiness
\t\tdataType: string
\t\tsourceColumn: LineOfBusiness

\tcolumn Region
\t\tdataType: string
\t\tsourceColumn: Region
"""

DIMDATE = """table DimDate
\tcolumn DateKey
\t\tdataType: int64
\t\tsourceColumn: DateKey

\tcolumn Year
\t\tdataType: int64
\t\tsourceColumn: Year

\tcolumn Month
\t\tdataType: int64
\t\tsourceColumn: Month

\tcolumn MonthName
\t\tdataType: string
\t\tsourceColumn: MonthName
"""

RELS = """relationship rel1
\tfromColumn: FactClaims.PolicyKey
\ttoColumn: DimPolicy.PolicyKey

relationship rel2
\tfromColumn: FactClaims.DateKey
\ttoColumn: DimDate.DateKey
"""


def make_model(root: Path, fact=FACT, dimpolicy=DIMPOLICY, dimdate=DIMDATE, rels=RELS, extra=None) -> Path:
    m = Path(root) / "model"
    t = m / "definition" / "tables"
    t.mkdir(parents=True, exist_ok=True)
    (t / "FactClaims.tmdl").write_text(fact, encoding="utf-8")
    (t / "DimPolicy.tmdl").write_text(dimpolicy, encoding="utf-8")
    (t / "DimDate.tmdl").write_text(dimdate, encoding="utf-8")
    (m / "definition" / "relationships.tmdl").write_text(rels, encoding="utf-8")
    for rel, text in (extra or {}).items():
        p = m / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return m


def make_reports(root: Path, fields=(("Measure", "FactClaims", "Total Paid"),), extra_visual=None) -> Path:
    r = Path(root) / "reports"
    v = r / "R1" / "definition" / "pages" / "p1" / "visuals" / "v1"
    v.mkdir(parents=True, exist_ok=True)
    projections = [{"field": {k: {"Expression": {"SourceRef": {"Entity": t}}, "Property": n}}}
                   for k, t, n in fields]
    body = {"visual": {"query": {"queryState": {"Values": {"projections": projections}}}}}
    if extra_visual:
        body["visual"].update(extra_visual)
    (v / "visual.json").write_text(json.dumps(body), encoding="utf-8")
    return r


def removed_columns(p: dict) -> set:
    return {(c["table"], c["column"]) for c in p["remove_columns"]}
