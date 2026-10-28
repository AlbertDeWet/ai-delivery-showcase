"""Generate three synthetic semantic models (DEV/UAT/PROD) for
"Contoso Insurance (fictional sample data)", plus their data and reports.

Deliberate problems planted:
  * a wide fact table (32 columns) where most columns are never used
  * high-cardinality text columns (narratives, hashes, GUID-like refs)
  * broken measures (missing column, missing measure, and transitive breaks)
  * drift between DEV, UAT and PROD
  * PBIR-style report visuals that reference fields
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb

from .tmdl import quote

ENVS = ["DEV", "UAT", "PROD"]
ROWS = {"DEV": 40_000, "UAT": 60_000, "PROD": 80_000}
SEED = {"DEV": 0.11, "UAT": 0.22, "PROD": 0.33}

# name -> (dataType, SQL expression over row id `i`)
FACT = {
    "ClaimKey": ("int64", "i"),
    "PolicyKey": ("int64", "1 + (hash(i, 1) % 5000)"),
    "DateKey": ("int64", "CAST(strftime(DATE '2023-01-01' + CAST(hash(i, 2) % 1000 AS INTEGER), '%Y%m%d') AS BIGINT)"),
    "ProductKey": ("int64", "1 + (hash(i, 3) % 12)"),
    "PaidAmount": ("double", "round((hash(i, 4) % 2000000) / 100.0, 2)"),
    "ReserveAmount": ("double", "round((hash(i, 5) % 900000) / 100.0, 2)"),
    "ClaimStatus": ("string", "['Open','Closed','Reopened','Declined'][1 + CAST(hash(i, 6) % 4 AS INTEGER)]"),
    "RecoveryAmount": ("double", "round((hash(i, 7) % 50000) / 100.0, 2)"),
    # ---- never used by any measure, relationship or visual ----
    "ClaimNarrative": ("string", "'Claimant reports ' || md5(CAST(i AS VARCHAR)) || ' ' || md5(CAST(i*7 AS VARCHAR))"),
    "AdjusterNote": ("string", "'Note ' || md5(CAST(i*3 AS VARCHAR))"),
    "ExternalClaimRef": ("string", "md5(CAST(i*11 AS VARCHAR))"),
    "SourceRowHash": ("string", "sha256(CAST(i AS VARCHAR))"),
    "LoadBatchId": ("int64", "CAST(i / 5000 AS BIGINT)"),
    "SourceFileName": ("string", "'claims_extract_' || CAST(i / 5000 AS VARCHAR) || '.csv'"),
    "IngestTimestamp": ("dateTime", "TIMESTAMP '2024-01-01' + to_seconds(CAST(i AS BIGINT))"),
    "ClaimantPostcode": ("string", "lpad(CAST(hash(i, 8) % 9000 AS VARCHAR), 4, '0')"),
    "LegacyClaimNo": ("string", "'LC-' || CAST(i + 900000 AS VARCHAR)"),
    "CauseCode": ("string", "'C' || CAST(hash(i, 9) % 40 AS VARCHAR)"),
    "SubCauseCode": ("string", "'S' || CAST(hash(i, 10) % 300 AS VARCHAR)"),
    "ExcessAmount": ("double", "round((hash(i, 11) % 100000) / 100.0, 2)"),
    "VatAmount": ("double", "round((hash(i, 12) % 300000) / 100.0, 2)"),
    "FeeAmount": ("double", "round((hash(i, 13) % 20000) / 100.0, 2)"),
    "BrokerCode": ("string", "'B' || CAST(hash(i, 14) % 800 AS VARCHAR)"),
    "ChannelCode": ("string", "['Direct','Broker','Online'][1 + CAST(hash(i, 15) % 3 AS INTEGER)]"),
    "RegionCode": ("string", "'R' || CAST(hash(i, 16) % 9 AS VARCHAR)"),
    "AuditUser": ("string", "'svc_loader_' || CAST(hash(i, 17) % 5 AS VARCHAR)"),
    "AuditTs": ("dateTime", "TIMESTAMP '2024-06-01' + to_seconds(CAST(hash(i, 18) % 10000000 AS BIGINT))"),
    "IsDeleted": ("boolean", "hash(i, 19) % 100 = 0"),
    "RowVersion": ("int64", "1 + hash(i, 20) % 3"),
    "Attr01": ("double", "(hash(i, 21) % 1000000) / 7.0"),
    "Attr02": ("double", "(hash(i, 22) % 1000000) / 13.0"),
    "Attr03": ("string", "md5(CAST(i*17 AS VARCHAR))"),
}
UAT_ONLY_FACT = {"TempFixFlag": ("boolean", "hash(i, 23) % 2 = 0")}  # drift

DIMS = {
    "DimPolicy": ("SELECT i AS PolicyKey, 'POL-' || lpad(CAST(i AS VARCHAR), 6, '0') AS PolicyNumber, "
                  "['Motor','Home','Commercial'][1 + CAST(i % 3 AS INTEGER)] AS LineOfBusiness, "
                  "md5(CAST(i AS VARCHAR)) AS PolicyHolderToken FROM range(1, 5001) t(i)",
                  {"PolicyKey": "int64", "PolicyNumber": "string", "LineOfBusiness": "string", "PolicyHolderToken": "string"}),
    "DimDate": ("SELECT CAST(strftime(d, '%Y%m%d') AS BIGINT) AS DateKey, year(d) AS Year, month(d) AS Month, "
                "strftime(d, '%b') AS MonthName FROM (SELECT DATE '2023-01-01' + CAST(i AS INTEGER) AS d FROM range(0, 1200) t(i))",
                {"DateKey": "int64", "Year": "int64", "Month": "int64", "MonthName": "string"}),
    "DimProduct": ("SELECT i AS ProductKey, 'Product ' || CAST(i AS VARCHAR) AS ProductName, "
                   "['Personal','Commercial'][1 + CAST(i % 2 AS INTEGER)] AS Segment FROM range(1, 13) t(i)",
                   {"ProductKey": "int64", "ProductName": "string", "Segment": "string"}),
}

GOOD_MEASURES = {
    "Total Paid": "SUM(FactClaims[PaidAmount])",
    "Total Reserve": "SUM(FactClaims[ReserveAmount])",
    "Incurred": "[Total Paid] + [Total Reserve]",
    "Claim Count": "COUNTROWS(FactClaims)",
    "Policies With Claims": "DISTINCTCOUNT(FactClaims[PolicyKey])",
    "Avg Paid per Claim": "DIVIDE([Total Paid], [Claim Count])",
    "Open Claims": 'CALCULATE(COUNTROWS(FactClaims), FactClaims[ClaimStatus] = "Open")',
    "Closed Paid": 'CALCULATE(SUM(FactClaims[PaidAmount]), FactClaims[ClaimStatus] = "Closed")',
    "Open Ratio": "DIVIDE([Open Claims], [Claim Count])",
    "Max Paid": "MAX(FactClaims[PaidAmount])",
    "Min Reserve": "MIN(FactClaims[ReserveAmount])",
    "Avg Reserve": "AVERAGE(FactClaims[ReserveAmount])",
    "Total Recoveries": "SUM(FactClaims[RecoveryAmount])",
    "Net Paid": "[Total Paid] - [Total Recoveries]",
    "Paid to Reserve": "DIVIDE([Total Paid], [Total Reserve])",
    # valid DAX outside the regression translator's subset -> graded UNTESTED, never "expected"
    "Avg Net per Claim": "AVERAGEX(FactClaims, FactClaims[PaidAmount] - FactClaims[RecoveryAmount])",
}
SORT_BY = {("DimDate", "MonthName"): "Month"}
BROKEN_MEASURES = {
    "Gross Reserve (old)": "SUM(FactClaims[GrossReserveOld])",            # missing column
    "Paid Last Year": "[Total Paid PY]",                                   # missing measure
    "Reserve Movement": "[Gross Reserve (old)] - [Total Reserve]",         # transitive
    "Legacy Severity": "DIVIDE([Gross Reserve (old)], [Claim Count])",    # transitive
    "Broker Paid": "SUM(FactClaims[BrokerPaidAmt])",                      # missing column
    "Paid Growth": "DIVIDE([Total Paid] - [Paid Last Year], [Paid Last Year])",  # transitive
    "Commission": "SUM(DimBroker[Commission])",                           # missing table
    "Commission Ratio": "DIVIDE([Commission], [Total Paid])",              # transitive
    "Old Loss Ratio": "DIVIDE([Incurred], [Earned Premium])",              # missing measure
    "Reopen Paid": 'CALCULATE(SUM(FactClaims[PaidAmt]), FactClaims[ClaimStatus] = "Reopened")',  # typo column
}


def env_measures(env: str) -> dict:
    m = dict(GOOD_MEASURES)
    m.update(BROKEN_MEASURES)
    if env == "UAT":
        m.pop("Commission Ratio")           # drift: someone half-fixed UAT
        m.pop("Old Loss Ratio")
    if env == "PROD":
        m.pop("Reopen Paid")
        m["Avg Paid per Claim"] = "DIVIDE(SUM(FactClaims[PaidAmount]), COUNTROWS(FactClaims))"  # drift: same result, different text
    return m


REPORTS = {
    "Claims Overview": [
        ("kpi_paid", [("measure", "FactClaims", "Total Paid")]),
        ("kpi_count", [("measure", "FactClaims", "Claim Count")]),
        ("paid_by_year", [("column", "DimDate", "Year"), ("measure", "FactClaims", "Total Paid")]),
        ("status_table", [("column", "FactClaims", "ClaimStatus"), ("measure", "FactClaims", "Claim Count"),
                          ("measure", "FactClaims", "Avg Paid per Claim")]),
    ],
    "Reserving": [
        ("reserve_by_lob", [("column", "DimPolicy", "LineOfBusiness"), ("measure", "FactClaims", "Total Reserve"),
                            ("measure", "FactClaims", "Incurred")]),
        ("open_ratio", [("measure", "FactClaims", "Open Ratio")]),
        ("ratio_by_month", [("column", "DimDate", "MonthName"), ("measure", "FactClaims", "Paid to Reserve")]),
    ],
    "Product Performance": [
        ("paid_by_product", [("column", "DimProduct", "ProductName"), ("column", "DimProduct", "Segment"),
                             ("measure", "FactClaims", "Net Paid")]),
        ("policy_slicer", [("column", "DimPolicy", "PolicyNumber")]),
        ("closed_paid", [("measure", "FactClaims", "Closed Paid"), ("measure", "FactClaims", "Policies With Claims")]),
    ],
}

RELATIONSHIPS = [
    ("FactClaims", "PolicyKey", "DimPolicy", "PolicyKey"),
    ("FactClaims", "DateKey", "DimDate", "DateKey"),
    ("FactClaims", "ProductKey", "DimProduct", "ProductKey"),
]


def _visual_json(fields) -> dict:
    projections = []
    for kind, table, name in fields:
        key = "Measure" if kind == "measure" else "Column"
        projections.append({"field": {key: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}}})
    return {"$schema": "synthetic-pbir-visual/1.0",
            "visual": {"query": {"queryState": {"Values": {"projections": projections}}}}}


def _table_tmdl(table: str, cols: dict, measures: dict | None = None) -> str:
    lines = [f"table {quote(table)}"]
    for name, expr in (measures or {}).items():
        lines += [f"\tmeasure {quote(name)} = {expr}", "\t\tformatString: #,0.00", ""]
    for name, dtype in cols.items():
        lines += [f"\tcolumn {quote(name)}", f"\t\tdataType: {dtype}", f"\t\tsourceColumn: {name}"]
        if (table, name) in SORT_BY:
            lines.append(f"\t\tsortByColumn: {SORT_BY[(table, name)]}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _write_skeleton(mdir: Path, tables: list) -> None:
    """Standard PBIP skeleton around the tables (synthetic, shape per the public TMDL docs)."""
    d = mdir / "definition"
    (d / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1601\n", encoding="utf-8")
    (d / "model.tmdl").write_text(
        "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n\n"
        "annotation PBI_ProTooling = [\"DevMode\"]\n\n"
        + "".join(f"ref table {quote(t)}\n" for t in tables) + "\nref culture en-US\n", encoding="utf-8")
    (d / "cultures").mkdir(exist_ok=True)
    (d / "cultures" / "en-US.tmdl").write_text(
        "cultureInfo en-US\n\n\tlinguisticMetadata =\n\t\t\t{\"Version\": \"1.0.0\", \"Language\": \"en-US\"}\n"
        "\t\tcontentType: json\n", encoding="utf-8")
    (mdir / "definition.pbism").write_text(json.dumps({"version": "4.2", "settings": {}}), encoding="utf-8")
    (mdir / ".platform").write_text(json.dumps({"metadata": {"type": "SemanticModel",
                                                             "displayName": "Contoso Claims (fictional)"}}),
                                    encoding="utf-8")


def write_data(con, env: str, out: Path, extra_rows: int = 0) -> None:
    """Write the env's tables as parquet. extra_rows simulates a fresh-data refresh."""
    out.mkdir(parents=True, exist_ok=True)
    fact = dict(FACT)
    if env == "UAT":
        fact.update(UAT_ONLY_FACT)
    n = ROWS[env] + extra_rows
    sel = ", ".join(f"{expr} AS {name}" for name, (_, expr) in fact.items())
    con.execute(f"COPY (SELECT {sel} FROM range(1, {n + 1}) t(i)) TO '{(out / 'FactClaims.parquet').as_posix()}' (FORMAT parquet)")
    for t, (sql, _) in DIMS.items():
        con.execute(f"COPY ({sql}) TO '{(out / (t + '.parquet')).as_posix()}' (FORMAT parquet)")


def generate(root: Path) -> None:
    root = Path(root)
    for sub in ("models", "data", "reports"):
        shutil.rmtree(root / sub, ignore_errors=True)
    con = duckdb.connect()
    for env in ENVS:
        tdir = root / "models" / env / "definition" / "tables"
        tdir.mkdir(parents=True)
        fact = {k: v[0] for k, v in FACT.items()}
        if env == "UAT":
            fact.update({k: v[0] for k, v in UAT_ONLY_FACT.items()})
        (tdir / "FactClaims.tmdl").write_text(_table_tmdl("FactClaims", fact, env_measures(env)), encoding="utf-8")
        for t, (_, cols) in DIMS.items():
            (tdir / f"{t}.tmdl").write_text(_table_tmdl(t, cols), encoding="utf-8")
        rel = []
        for i, (ft, fc, tt, tc) in enumerate(RELATIONSHIPS, 1):
            rel += [f"relationship rel{i}", f"\tfromColumn: {ft}.{fc}", f"\ttoColumn: {tt}.{tc}", ""]
        (root / "models" / env / "definition" / "relationships.tmdl").write_text("\n".join(rel), encoding="utf-8")
        _write_skeleton(root / "models" / env, ["FactClaims", *DIMS])
        write_data(con, env, root / "data" / env / "v1")
    for report, visuals in REPORTS.items():
        for vname, fields in visuals:
            vdir = root / "reports" / report / "definition" / "pages" / "page1" / "visuals" / vname
            vdir.mkdir(parents=True)
            (vdir / "visual.json").write_text(json.dumps(_visual_json(fields), indent=2), encoding="utf-8")
    con.close()
