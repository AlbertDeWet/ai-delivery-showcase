"""Token-based DAX reference analysis with row-context scoping and a function allowlist.

Rules (certainty first):
  * T[x]           -> column T[x], else measure x (table-qualified measure ref), else missing.
  * [x] (bare)     -> measure x; else a column in the CURRENT row context:
                      - certain context (iterator over a bare table name, or a calculated
                        column / RLS filter on its own table): resolved or missing;
                      - uncertain context (iterator over ALL(T), RELATEDTABLE(T), FILTER(...),
                        a VAR, any other table expression): the column is treated as USED
                        and the measure is NOT flagged broken, unless no table has column x.
                      - no row context: missing.
  * Bare identifier as a whole table argument (COUNTROWS(T), FILTER(T, ...)): table ref,
    unless it is a VAR defined EARLIER in the expression.
"""
from __future__ import annotations

import re

from .tmdl import Model, unquote

DAX_FUNCTIONS = set("""
ABS ADDCOLUMNS ALL ALLEXCEPT ALLNOBLANKROW ALLSELECTED AND AVERAGE AVERAGEA AVERAGEX BLANK CALCULATE
CALCULATETABLE CEILING COALESCE CONCATENATE CONCATENATEX CONTAINS CONTAINSSTRING COUNT COUNTA COUNTAX
COUNTBLANK COUNTROWS COUNTX CROSSFILTER DATE DATEADD DATESBETWEEN DATESMTD DATESQTD DATESYTD DAY DISTINCT
DISTINCTCOUNT DIVIDE EARLIER EDATE EOMONTH FALSE FILTER FIRSTNONBLANK FLOOR FORMAT GENERATE HASONEVALUE IF
IFERROR INT ISBLANK ISEMPTY ISFILTERED ISINSCOPE KEEPFILTERS LASTDATE LASTNONBLANK LEFT LEN LOWER MAX MAXA MAXX
MIN MINA MINX MOD MONTH NAMEOF NOT OR PARALLELPERIOD PREVIOUSMONTH PREVIOUSYEAR PRODUCTX RANKX RELATED
RELATEDTABLE REMOVEFILTERS RIGHT ROUND SAMEPERIODLASTYEAR SEARCH SELECTCOLUMNS SELECTEDMEASURE
SELECTEDMEASURENAME SELECTEDVALUE SUM SUMMARIZE SUMX SWITCH TODAY TOPN TOTALYTD TREATAS TRUE UPPER
USERELATIONSHIP USERPRINCIPALNAME VALUES YEAR
""".split())
ITERATORS = {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "PRODUCTX", "RANKX", "CONCATENATEX",
             "FILTER", "ADDCOLUMNS", "SELECTCOLUMNS", "GENERATE"}
TABLE_ARG0 = ITERATORS | {"COUNTROWS", "ALL", "ALLNOBLANKROW", "ALLSELECTED", "ALLEXCEPT", "REMOVEFILTERS",
                          "VALUES", "DISTINCT", "RELATEDTABLE", "CALCULATETABLE", "SUMMARIZE", "ISEMPTY",
                          "KEEPFILTERS"}

_TOKEN = re.compile(r'''(?P<ws>\s+)|(?P<S>"(?:[^"]|"")*")|(?P<Q>'(?:[^']|'')*')|(?P<B>\[(?:[^\]]|\]\])*\])|'''
                    r'''(?P<I>[^\W\d][\w.]*)|(?P<N>\d+(?:\.\d+)?)|(?P<P>&&|\|\||<=|>=|<>|==|.)''', re.S)


def strip_comments(expr: str) -> str:
    """Remove // -- and /* */ comments; strings, [bracketed] and 'quoted' names are kept verbatim."""
    out, pos = [], 0
    expr = expr or ""
    while pos < len(expr):
        if expr.startswith("//", pos) or expr.startswith("--", pos):
            j = expr.find("\n", pos)
            pos = len(expr) if j < 0 else j
            continue
        if expr.startswith("/*", pos):
            j = expr.find("*/", pos + 2)
            pos = len(expr) if j < 0 else j + 2
            continue
        m = _TOKEN.match(expr, pos)
        out.append(m.group(0))
        pos = m.end()
    return "".join(out)


def tokens(expr: str) -> list:
    out = []
    for m in _TOKEN.finditer(strip_comments(expr)):
        if m.lastgroup != "ws":
            out.append((m.lastgroup, m.group(0)))
    return out


def _bname(t: str) -> str:
    return t[1:-1].replace("]]", "]")


def analyse(model: Model, expr: str, row_table: str | None = None) -> dict:
    """{'columns', 'measures', 'tables', 'missing', 'functions', 'bare'}

    Scoping: every '(' opens a lexical frame (function call or plain group). A VAR is
    visible from its definition to the end of the frame it was defined in. Row context
    for a bare [col] comes only from the iterator frames currently open."""
    toks = tokens(expr)
    out = {"columns": set(), "measures": set(), "tables": set(), "missing": [], "functions": set(),
           "bare": set()}
    frames = []          # {"func": NAME or None, "arg": i, "start": tok index, "ctx": (table, certain) or None}
    vars_ = []           # (lower name, frame depth at definition)
    base = [(row_table, True)] if row_table else []
    n = len(toks)
    k = 0

    def is_var(name):
        return name.lower() in {v for v, _ in vars_}

    def ctx_of(first):
        """Row context supplied by an iterator's first argument."""
        if len(first) == 1 and first[0][0] in ("I", "Q"):
            name = unquote(first[0][1])
            if is_var(name):
                return (None, False)
            t = model.table(name)
            return (t, True) if t else (None, False)
        # table expression (ALL(T), RELATEDTABLE(T), FILTER(T, ...), ...): the first plain
        # table name inside it is the context table, but this is not certain
        for j, (typ, text) in enumerate(first):
            nxt = first[j + 1][1] if j + 1 < len(first) else ""
            if typ in ("I", "Q") and nxt not in ("(",) and not nxt.startswith("["):
                name = unquote(text)
                if not is_var(name) and model.table(name):
                    return (model.table(name), False)
        return (None, False)

    while k < n:
        typ, text = toks[k]
        nxt = toks[k + 1] if k + 1 < n else ("", "")
        prev = toks[k - 1] if k else ("", "")
        if typ == "I" and text.upper() == "VAR" and nxt[0] == "I":
            vars_.append((nxt[1].lower(), len(frames)))
            k += 2
            continue
        if typ == "I" and nxt[1] == "(":
            out["functions"].add(text.upper())
            frames.append({"func": text.upper(), "arg": 0, "start": k + 2, "ctx": None})
            k += 2
            continue
        if text == "(" and typ == "P":
            frames.append({"func": None, "arg": 0, "start": k + 1, "ctx": None})
            k += 1
            continue
        if typ in ("I", "Q") and nxt[0] == "B":
            t, c = unquote(text), _bname(nxt[1])
            if is_var(t):
                pass  # column of a table variable: lineage unknown, nothing pinned
            elif model.table(t) is None:
                out["missing"].append(f"table {t} (in {t}[{c}])")
            elif (key := model.column(t, c)):
                out["columns"].add(key)
            elif (m := model.measure(c)):
                out["measures"].add(m)
            else:
                out["missing"].append(f"{t}[{c}]")
            k += 2
            continue
        if typ == "B":
            x = _bname(text)
            out["bare"].add(x)
            if (m := model.measure(x)):
                out["measures"].add(m)
            else:
                ctxs = base + [f["ctx"] for f in frames if f["func"] in ITERATORS and f["arg"] >= 1 and f["ctx"]]
                found = [key for t, _ in ctxs if t and (key := model.column(t, x))]
                if not found and any(t is None for t, _ in ctxs):
                    # unknown table expression: keep any same-named column, never call it broken
                    found = [key for key in model.columns if key[1].lower() == x.lower()]
                if found:
                    out["columns"].update(found)
                else:
                    out["missing"].append(f"[{x}]")
            k += 1
            continue
        if typ in ("I", "Q") and frames and prev[1] in ("(", ",") and nxt[1] in (",", ")"):
            f = frames[-1]
            name = unquote(text)
            if f["func"] in TABLE_ARG0 and f["arg"] == 0 and not is_var(name):
                t = model.table(name)
                if t:
                    out["tables"].add(t)
                else:
                    out["missing"].append(f"table {name}")
            k += 1
            continue
        if text == "," and frames:
            f = frames[-1]
            if f["arg"] == 0 and f["func"] in ITERATORS:
                f["ctx"] = ctx_of(toks[f["start"]:k])
            f["arg"] += 1
        elif text == ")" and frames:
            frames.pop()
            vars_ = [(v, d) for v, d in vars_ if d <= len(frames)]
        k += 1
    out["missing"] = sorted(set(out["missing"]))
    return out
