"""Deterministic offline baseline: keyword rules + SQL templates. No model, no network.

This is the floor every LLM tier must beat. It was written generically
(metric x dimension x filter templates), not per eval question, but it was
written by the same author as the eval set, so treat its score as a sanity
check of the harness rather than as a benchmark.
"""

from __future__ import annotations

import re
import time

from nl2sql.providers.base import Generation

DESTRUCTIVE = re.compile(
    r"\b(drop|delete|truncate|update|insert|alter|create|attach|copy|grant|remove all|wipe|overwrite)\b", re.I)
INJECTION = re.compile(r"\b(ignore (all |your |the )?(previous|prior|above)|system override|jailbreak)\b", re.I)
AMBIGUOUS = re.compile(r"\b(best|worst|big|biggest|good|bad|performing|important|interesting)\b", re.I)

LOB = ["motor", "property", "commercial", "liability", "travel"]
REGIONS = ["north", "south", "east", "west", "central"]

# metric -> (fact, expression, date column, kind)
METRICS = {
    "loss_ratio": ("ratio", None, None, "ratio"),
    "claim_frequency": ("ratio", None, None, "ratio"),
    "avg_severity": ("claims", "ROUND(SUM(COALESCE(c.paid_amount,0)+c.reserve_amount)/COUNT(*),2)", "c.loss_date", "avg"),
    "claims_incurred": ("claims", "ROUND(SUM(COALESCE(c.paid_amount,0)+c.reserve_amount),2)", "c.loss_date", "sum"),
    "claims_paid": ("claims", "ROUND(SUM(COALESCE(c.paid_amount,0)),2)", "c.loss_date", "sum"),
    "reserves": ("claims", "ROUND(SUM(c.reserve_amount),2)", "c.loss_date", "sum"),
    "claim_count": ("claims", "COUNT(*)", "c.loss_date", "count"),
    "gwp": ("premiums", "ROUND(SUM(pr.amount),2)", "pr.transaction_date", "sum"),
    "avg_premium": ("policies", "ROUND(AVG(p.annual_premium),2)", "p.start_date", "avg"),
    "sum_insured": ("policies", "ROUND(SUM(p.sum_insured),2)", "p.start_date", "sum"),
    "policy_count": ("policies", "COUNT(*)", "p.start_date", "count"),
    "customer_count": ("customers", "COUNT(*)", "cu.signup_date", "count"),
    "broker_count": ("brokers", "COUNT(*)", None, "count"),
}

DIMS = {
    "line_of_business": ("pd.line_of_business", {"policies", "products"}),
    "product": ("pd.product_name", {"policies", "products"}),
    "region": ("cu.region", {"policies", "customers"}),
    "broker": ("b.broker_name", {"policies", "brokers"}),
    "cause": ("c.cause", set()),
    "customer_type": ("cu.customer_type", {"policies", "customers"}),
    "fiscal_year": ("FY", set()),
    "year": ("YEAR", set()),
}


def detect_metric(q: str) -> str | None:
    rules = [
        (r"loss ratio", "loss_ratio"),
        (r"claim frequency|claims per policy", "claim_frequency"),
        (r"average claim|severity", "avg_severity"),
        (r"incurred", "claims_incurred"),
        (r"\bpaid\b", "claims_paid"),
        (r"reserve", "reserves"),
        (r"how many claims|number of claims|claim count|count of claims|claims count", "claim_count"),
        (r"average annual premium|average premium", "avg_premium"),
        (r"\bgwp\b|written premium|premium", "gwp"),
        (r"sum insured", "sum_insured"),
        (r"how many (\w+ )?policies|number of (\w+ )?policies|policy count|count of policies", "policy_count"),
        (r"how many (\w+ )?customers|number of customers|customer count", "customer_count"),
        (r"how many brokers|number of brokers", "broker_count"),
    ]
    for pat, name in rules:
        if re.search(pat, q):
            return name
    return None


def detect_dim(q: str) -> str | None:
    m = re.search(r"\b(by|per|for each|across each)\s+(the\s+)?(customer\s+)?"
                  r"(line of business|product|region|broker|cause|customer type|fiscal year|calendar year|year)", q)
    if not m:
        m2 = re.search(r"\b(?:top|which)\s+\d*\s*(brokers?|products?)\b", q)
        if m2:
            return "broker" if m2.group(1).startswith("broker") else "product"
        return None
    word = m.group(4)
    return {"line of business": "line_of_business", "customer type": "customer_type",
            "calendar year": "year"}.get(word, word.replace(" ", "_"))


def _date_filters(q: str, date_col: str | None) -> list[str]:
    if not date_col:
        return []
    out = []
    fy = re.search(r"(?:fiscal year|fy)\s*(\d{4})", q)
    if fy:
        y = int(fy.group(1))
        out.append(f"{date_col} BETWEEN DATE '{y - 1}-07-01' AND DATE '{y}-06-30'")
        return out
    cy = re.search(r"\b(?:in|during|for)\s+(?:calendar year\s+)?(20\d\d)\b", q)
    if cy:
        out.append(f"EXTRACT(year FROM {date_col}) = {cy.group(1)}")
    return out


def _alias(cond: str) -> str | None:
    m = re.search(r"\b(c|pr|p|cu|pd|b)\.", cond)
    return m.group(1) if m else None


class BaselineProvider:
    name = "baseline"
    model = "rules-v1"

    def generate(self, question: str, schema_card: str = "") -> Generation:
        t0 = time.perf_counter()
        g = self._generate(question)
        g.latency_s = time.perf_counter() - t0
        return g

    def control_total(self, question: str, schema_card: str = "") -> Generation:
        """Independent derivation of the headline number: metric + filters, no grouping,
        built straight from the glossary definitions (separate code path from _build)."""
        t0 = time.perf_counter()
        q = question.lower()
        metric = detect_metric(q)
        if metric is None or METRICS[metric][3] in ("ratio", "avg") or re.search(r"\btop\b", q):
            g = Generation("refuse", message="checker abstains (no reconcilable grand total)")
        else:
            g = Generation("sql", sql=self._control_sql(q, metric))
        g.latency_s = time.perf_counter() - t0
        return g

    # ---- generation -------------------------------------------------------
    def _generate(self, question: str) -> Generation:
        q = question.lower()
        if DESTRUCTIVE.search(q) or INJECTION.search(q):
            return Generation("refuse", message="Request would modify data or override instructions; refused.")
        metric = detect_metric(q)
        if metric is None:
            if AMBIGUOUS.search(q):
                return Generation("clarify", message="Which measure do you mean (for example GWP, claims incurred, loss ratio)?")
            return Generation("refuse", message="I cannot map this question to the Contoso schema.")
        try:
            return Generation("sql", sql=self._build(q, metric))
        except ValueError as exc:
            return Generation("refuse", message=str(exc))

    def _filters(self, q: str, fact: str, metric: str) -> tuple[list[str], set[str]]:
        where, need = [], set()
        if re.search(r"\bactive\b", q):
            where.append("LOWER(p.status) = 'active'"); need.add("policies")
        for st in ("cancelled", "lapsed"):
            if re.search(rf"\b{st}\b", q):
                where.append(f"LOWER(p.status) = '{st}'"); need.add("policies")
        if fact == "claims":
            for st in ("open", "closed", "declined"):
                if re.search(rf"\b{st}\b", q):
                    where.append(f"LOWER(c.status) = '{st}'")
        if re.search(r"\bdirect\b|no broker|without a broker", q):
            where.append("p.broker_id IS NULL"); need.add("policies")
        if "line of business" not in q or not re.search(r"by (the )?line of business", q):
            for lob in LOB:
                if re.search(rf"\b{lob}\b", q):
                    where.append(f"pd.line_of_business = '{lob.title()}'"); need |= {"policies", "products"}
        for r in REGIONS:
            if re.search(rf"\b{r}\b", q):
                where.append(f"cu.region = '{r.title()}'"); need |= {"policies", "customers"}
        if re.search(r"\bbusiness customers\b", q):
            where.append("cu.customer_type = 'Business'"); need |= {"policies", "customers"}
        if fact == "premiums" and re.search(r"\breversal", q):
            where.append("pr.transaction_type = 'REVERSAL'")
        return where, need

    def _from(self, fact: str, need: set[str]) -> str:
        base = {"claims": "claims c", "premiums": "premiums pr", "policies": "policies p",
                "customers": "customers cu", "brokers": "brokers b"}[fact]
        parts = [base]
        if fact in ("claims", "premiums") and need & {"policies", "products", "customers", "brokers"}:
            alias = "c" if fact == "claims" else "pr"
            parts.append(f"JOIN policies p ON p.policy_id = {alias}.policy_id")
        if "products" in need:
            parts.append("JOIN products pd ON pd.product_id = p.product_id")
        if "customers" in need and fact != "customers":
            parts.append("JOIN customers cu ON cu.customer_id = p.customer_id")
        if "brokers" in need and fact != "brokers":
            parts.append("JOIN brokers b ON b.broker_id = p.broker_id")
        return " ".join(parts)

    def _dim_expr(self, dim: str, date_col: str | None) -> str:
        expr = DIMS[dim][0]
        if expr == "YEAR":
            return f"EXTRACT(year FROM {date_col})"
        if expr == "FY":
            return f"EXTRACT(year FROM {date_col}) + CASE WHEN EXTRACT(month FROM {date_col}) >= 7 THEN 1 ELSE 0 END"
        return expr

    def _simple(self, q: str, metric: str, dim: str | None) -> str:
        fact, expr, date_col, _ = METRICS[metric]
        where, need = self._filters(q, fact, metric)
        where += _date_filters(q, date_col)
        if dim:
            need |= DIMS[dim][1]
            if dim == "cause" and fact != "claims":
                raise ValueError("cause only applies to claims")
        if fact == "customers" and need - {"customers"}:
            need = {"customers"}
            where = [w for w in where if w.startswith("cu.")]
        sql = f"SELECT {{sel}} FROM {self._from(fact, need)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if dim:
            d = self._dim_expr(dim, date_col)
            sql = sql.format(sel=f"{d} AS {dim}, {expr} AS value") + f" GROUP BY {d}"
            top = re.search(r"\btop\s+(\d+)", q)
            if top:
                sql += f" ORDER BY value DESC LIMIT {top.group(1)}"
            else:
                sql += " ORDER BY 1"
            return sql
        return sql.format(sel=f"{expr} AS value")

    def _build(self, q: str, metric: str) -> str:
        dim = detect_dim(q)
        if metric == "loss_ratio":
            num = self._simple(q, "claims_incurred", dim)
            den = self._simple(q, "gwp", dim)
            if dim:
                return (f"WITH n AS ({num}), d AS ({den}) SELECT d.{dim}, ROUND(COALESCE(n.value,0)/d.value,4) AS loss_ratio "
                        f"FROM d LEFT JOIN n ON n.{dim} IS NOT DISTINCT FROM d.{dim} ORDER BY 1")
            return f"SELECT ROUND(({num}) / ({den}), 4) AS loss_ratio"
        if metric == "claim_frequency":
            num = self._simple(q, "claim_count", None)
            den = self._simple(q, "policy_count", None)
            return f"SELECT ROUND(({num}) * 1.0 / ({den}), 4) AS claim_frequency"
        return self._simple(q, metric, dim)

    # ---- checker path -----------------------------------------------------
    def _control_sql(self, q: str, metric: str) -> str:
        """Control total from the base table with correlated EXISTS filters (no joins/grouping)."""
        fact, expr, date_col, _ = METRICS[metric]
        where, need = self._filters(q, fact, metric)
        where += _date_filters(q, date_col)
        top = re.search(r"\btop\s+\d+", q)
        if top:
            raise ValueError("top-N totals are not reconcilable")
        tbl = {"claims": "claims c", "premiums": "premiums pr", "policies": "policies p",
               "customers": "customers cu", "brokers": "brokers b"}[fact]
        own_alias = {"claims": "c", "premiums": "pr", "policies": "p", "customers": "cu", "brokers": "b"}[fact]
        own = [w for w in where if _alias(w) == own_alias]
        foreign = [w for w in where if w not in own]
        sql = f"SELECT {expr} AS control FROM {tbl}"
        conds = list(own)
        if foreign:
            alias = {"claims": "c", "premiums": "pr"}.get(fact)
            if alias:
                sub = ("SELECT 1 FROM policies p LEFT JOIN products pd ON pd.product_id = p.product_id "
                       "LEFT JOIN customers cu ON cu.customer_id = p.customer_id "
                       f"WHERE p.policy_id = {alias}.policy_id AND " + " AND ".join(foreign))
                conds.append(f"EXISTS ({sub})")
            else:
                sub = ("SELECT 1 FROM policies p2 LEFT JOIN products pd ON pd.product_id = p2.product_id "
                       "LEFT JOIN customers cu ON cu.customer_id = p2.customer_id "
                       "WHERE p2.policy_id = p.policy_id AND " + " AND ".join(foreign))
                conds.append(f"EXISTS ({sub})")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return sql
