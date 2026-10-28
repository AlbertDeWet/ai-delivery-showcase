"""Before/after regression suite.

Each model measure is translated from a small DAX subset to SQL and evaluated
with DuckDB over the synthetic data, exposed through views that contain ONLY
the columns the model defines. So if a trim removes a column that a measure
or a join needs, the query fails - exactly what we want to catch.

Supported DAX subset: SUM, AVERAGE, MIN, MAX, DISTINCTCOUNT, COUNTROWS,
DIVIDE, CALCULATE(agg, T[C] = "literal"), [Measure] refs, + - * / and numbers.
This is a DAX-equivalent check, not a DAX engine.

Each difference is classified by running the AFTER model on the BEFORE data:
  model effect = before            vs after-model on before-data
  data effect  = after-model/before-data vs after-model on after-data
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb

from .analyze import broken_measures, normalise, strip_comments
from .tmdl import Model, load_model, unquote

REL_TOL = 1e-9


class Untranslatable(Exception):
    pass


def _q(t, c=None):
    return f'"{t}"' if c is None else f'"{t}"."{c}"'


def _split_args(s: str) -> list:
    depth, cur, out = 0, "", []
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [a.strip() for a in out]


def _rewrite_func(sql: str, func: str, fn) -> str:
    pat = re.compile(r"\b" + func + r"\s*\(", re.I)
    while True:
        m = pat.search(sql)
        if not m:
            return sql
        start, i, depth = m.start(), m.end(), 1
        while depth:
            if i >= len(sql):
                raise Untranslatable(f"unbalanced parentheses in {func}")
            depth += {"(": 1, ")": -1}.get(sql[i], 0)
            i += 1
        sql = sql[:start] + fn(_split_args(sql[m.end():i - 1])) + sql[i:]


_TBL = r"('(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)"


def translate(model: Model, name: str, _stack=()) -> str:
    canon = model.measure(name)
    if canon is None:
        raise Untranslatable(f"missing measure [{name}]")
    name = canon
    if name in _stack:
        raise Untranslatable(f"circular reference via [{name}]")
    expr = strip_comments(model.measures[name].expression)

    def colref(m):
        t, c = unquote(m.group(1)), m.group(2)
        if model.table(t) and (k := model.column(t, c)):
            return _q(*k)
        if model.table(t) and model.measure(c):
            return "(" + translate(model, c, _stack + (name,)) + ")"
        return _q(t, c)  # unresolved: the SQL will fail, which is the point

    def calc(m):
        agg, inner, t, c, v = m.groups()
        cond = f"{_q(unquote(t), c)} = '{v}'"
        if agg.upper() == "COUNTROWS":
            return f"CAST(COUNT(CASE WHEN {cond} THEN 1 END) AS DOUBLE)"
        it, ic = re.match(_TBL + r"\[([^\]]+)\]", inner.strip()).groups()
        func = "AVG" if agg.upper() == "AVERAGE" else agg.upper()
        return f"{func}(CASE WHEN {cond} THEN {_q(unquote(it), ic)} END)"

    expr = re.sub(r"CALCULATE\(\s*(SUM|COUNTROWS|AVERAGE|MIN|MAX)\(([^()]*)\)\s*,\s*" + _TBL +
                  r"\[([^\]]+)\]\s*=\s*\"([^\"]*)\"\s*\)", calc, expr, flags=re.I)
    if re.search(r"\bCALCULATE\b", expr, re.I):
        raise Untranslatable("unsupported CALCULATE form")
    expr = re.sub(r"COUNTROWS\(\s*" + _TBL + r"\s*\)",
                  lambda m: "CAST(COUNT(*) AS DOUBLE)", expr, flags=re.I)
    expr = re.sub(_TBL + r"\[([^\]]+)\]", colref, expr)
    expr = re.sub(r"\[([^\]]+)\]", lambda m: "(" + translate(model, m.group(1), _stack + (name,)) + ")", expr)
    expr = _rewrite_func(expr, "DISTINCTCOUNT", lambda a: f"CAST(COUNT(DISTINCT {a[0]}) AS DOUBLE)")
    expr = _rewrite_func(expr, "DIVIDE", lambda a: f"(({a[0]}) / NULLIF(({a[1]}), 0))")
    expr = re.sub(r"\bAVERAGE\s*\(", "AVG(", expr, flags=re.I)
    leftover = re.sub(r'"[^"]*"|\b(SUM|AVG|MIN|MAX|COUNT|DISTINCT|CAST|AS|DOUBLE|NULLIF|CASE|WHEN|THEN|END)\b', "", expr)
    if re.search(r"[A-Za-z_]\w*\s*\(", leftover):
        raise Untranslatable(f"unsupported function in: {model.measures[name].expression}")
    return expr


FACT_TABLE = "FactClaims"
# grouping checks: exercise every dimension join (date, policy, product)
GROUPINGS = [("by_year", "DimDate", "Year"), ("by_lob", "DimPolicy", "LineOfBusiness"),
             ("by_product", "DimProduct", "Segment")]


def _load(con, model: Model, data_dir: Path) -> set:
    loaded = set()
    for t in model.tables:
        f = Path(data_dir) / f"{t}.parquet"
        if not f.exists():
            continue
        avail = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{f.as_posix()}'").fetchall()}
        cols = [c for (tt, c) in model.columns if tt == t and c in avail]
        if cols:
            con.execute(f"CREATE TABLE {_q(t)} AS SELECT {', '.join(_q(c) for c in cols)} FROM '{f.as_posix()}'")
            loaded.add(t)
    return loaded


def run_suite(model_dir: Path, data_dir: Path) -> dict:
    """check_id -> {'value', 'def'} or {'error', 'def', 'broken'?}.

    'def' is the fully expanded definition (SQL, or the normalised DAX when
    untranslatable) so the grader can tell a definition change from fresh data."""
    model = load_model(model_dir)
    broken = broken_measures(model)
    con = duckdb.connect()
    loaded = _load(con, model, data_dir)
    joins = " ".join(
        f"LEFT JOIN {_q(r.to_table)} ON {_q(r.from_table, r.from_column)} = {_q(r.to_table, r.to_column)}"
        for r in model.relationships if r.from_table == FACT_TABLE and r.to_table in loaded)
    groupings = [("total", None)] + [(g, (t, c)) for g, t, c in GROUPINGS if model.column(t, c)]
    results = {}
    for t in sorted(model.tables):
        try:
            results[f"rowcount::{t}"] = {"value": con.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0],
                                         "def": "rowcount"}
        except Exception as e:  # noqa: BLE001
            results[f"rowcount::{t}"] = {"error": str(e).splitlines()[0], "def": "rowcount"}
    for name in sorted(model.measures):
        if name in broken:
            for g, _ in groupings:
                results[f"{g}::{name}"] = {"error": f"broken: {broken[name]}", "broken": True,
                                           "def": normalise(model.measures[name].expression)}
            continue
        try:
            sql = translate(model, name)
        except Untranslatable as e:
            for g, _ in groupings:
                results[f"{g}::{name}"] = {"error": f"untranslatable: {e}",
                                           "def": normalise(model.measures[name].expression)}
            continue
        for g, by in groupings:
            try:
                if by is None:
                    val = con.execute(f"SELECT {sql} FROM {_q(FACT_TABLE)} {joins}").fetchone()[0]
                else:
                    q = (f"SELECT CAST({_q(*by)} AS VARCHAR) AS k, {sql} AS v FROM {_q(FACT_TABLE)} {joins} "
                         f"GROUP BY 1 ORDER BY 1 NULLS FIRST")
                    val = [list(r) for r in con.execute(q).fetchall()]
                results[f"{g}::{name}"] = {"value": val, "def": sql}
            except Exception as e:  # noqa: BLE001
                results[f"{g}::{name}"] = {"error": str(e).splitlines()[0], "def": sql}
    con.close()
    return results


def _close(a, b) -> bool:
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(a - b) <= REL_TOL * max(1.0, abs(a), abs(b))
    return a == b


def classify(before: dict, after_on_old: dict, after: dict) -> list:
    """IDENTICAL / EXPECTED (with reason) / UNTESTED / NEW / UNEXPECTED per check.

    'fresh data' is only possible when the definition is unchanged AND the after
    model reproduces the before result on the frozen before-data."""
    rows = []
    for cid in sorted(set(before) | set(after_on_old) | set(after)):
        b, ao, a = before.get(cid), after_on_old.get(cid), after.get(cid)
        if b is None:
            cls, why = "NEW", "new check (object did not exist before)"
        elif "error" in b:
            if b.get("broken"):
                if a is None and ao is None:
                    cls, why = "EXPECTED", "broken before; removed by trim"
                elif (a and "error" in a) or (ao and "error" in ao):
                    cls, why = "EXPECTED", "broken before and after (not a regression)"
                else:
                    cls, why = "EXPECTED", "broken before, resolves after"
            elif a is None or ao is None:
                cls, why = "UNEXPECTED", "valid but untested measure disappeared: " + b["error"]
            elif a.get("broken") or ao.get("broken"):
                cls, why = "UNEXPECTED", "valid but untested measure is broken after the change"
            elif not (b.get("def") == ao.get("def") == a.get("def")):
                cls, why = "UNEXPECTED", "untested measure definition changed"
            else:
                cls, why = "UNTESTED", "could not be evaluated: " + b["error"]
        elif a is None or ao is None:
            cls, why = "UNEXPECTED", "check disappeared after trim"
        elif b.get("def") != ao.get("def"):
            cls, why = "UNEXPECTED", "measure definition changed"
        elif "error" in ao or "error" in a:
            cls, why = "UNEXPECTED", "worked before, fails after: " + (ao.get("error") or a.get("error"))
        elif b["value"] != ao["value"] and not _close(b["value"], ao["value"]):
            cls, why = "UNEXPECTED", "model change altered the result on frozen data"
        elif b["value"] == a["value"]:
            cls, why = "IDENTICAL", ""
        elif b["value"] != ao["value"] or _close(ao["value"], a["value"]):
            cls, why = "EXPECTED", "noise: float difference within tolerance"
        else:
            cls, why = "EXPECTED", "fresh data: same definition, unchanged on frozen data"
        rows.append({"check": cid, "class": cls, "reason": why})
    return rows
