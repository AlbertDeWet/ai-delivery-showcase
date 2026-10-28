"""Static analysis of a TMDL model: references, broken measures, unused columns,
size ESTIMATE, consumer (report) check and cross-environment drift."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import duckdb

from .tmdl import Model, load_model, unquote

from .dax import DAX_FUNCTIONS, analyse, strip_comments  # noqa: F401  (strip_comments re-exported)


def expr_refs(expr: str) -> tuple[set, set, set]:
    """Syntactic references: ({(table, name)}, {bare [name]}, {table-arg names}) - no model needed."""
    from .dax import tokens, _bname
    toks = tokens(expr)
    cols, bare = set(), set()
    for i, (typ, text) in enumerate(toks):
        if typ == "B":
            if i and toks[i - 1][0] in ("I", "Q"):
                cols.add((unquote(toks[i - 1][1]), _bname(text)))
            else:
                bare.add(_bname(text))
    return cols, bare, set()


def resolve(model: Model, expr: str, row_table: str | None = None) -> dict:
    """Model-aware references (see smguard/dax.py for the scoping rules)."""
    return analyse(model, expr, row_table)


def measure_text(m) -> str:
    """Everything a measure evaluates: expression, dynamic format string, detail rows."""
    parts = [m.expression, m.format_expression, m.detail_expression]
    return "\n".join(p for p in parts if p)


def dax_texts(model: Model):
    """(where, DAX, row_table) for every DAX expression in the model."""
    for name, m in model.measures.items():
        yield f"measure [{name}]", measure_text(m), None
    for (t, c), col in model.columns.items():
        if col.expression:
            yield f"calculated column {t}[{c}]", col.expression, t
    for role, table, expr in model.roles:
        yield f"role {role} on {table}", expr, model.table(table)
    for why, expr in model.dax_sources:
        yield why, expr, None


def unsupported_dax(model: Model) -> list:
    out = []
    for where, expr, _ in dax_texts(model):
        bad = sorted(analyse(model, expr)["functions"] - DAX_FUNCTIONS)
        if bad:
            out.append(f"{where}: DAX function not allowlisted: {', '.join(bad)}")
    return out


def broken_measures(model: Model) -> dict:
    """measure -> reason. Includes transitive breaks and reference cycles."""
    broken, deps = {}, {}
    for name, m in model.measures.items():
        r = resolve(model, measure_text(m))
        deps[name] = r["measures"]
        if r["missing"]:
            broken[name] = "missing " + ", ".join(r["missing"])
    changed = True
    while changed:
        changed = False
        for name, ds in deps.items():
            if name not in broken:
                bad = sorted(d for d in ds if d in broken)
                if bad:
                    broken[name] = "depends on broken " + ", ".join(f"[{b}]" for b in bad)
                    changed = True
    for name in deps:
        if name in broken:
            continue
        seen, stack = set(), list(deps[name])
        while stack:
            d = stack.pop()
            if d == name:
                broken[name] = "circular reference"
                break
            if d in seen or d not in deps:
                continue
            seen.add(d)
            stack.extend(deps[d])
    return broken


def _walk_fields(o, parent=None, aliases=None):
    """Yield (kind, entity, property) for every field binding anywhere in a PBIR JSON tree:
    projections, Aggregation (implicit measures), filterConfig at any level, bookmarks
    (From/Source aliases), and measure references inside visual calculations ('vcalc')."""
    aliases = dict(aliases or {})
    if isinstance(o, dict):
        frm = o.get("From")
        if isinstance(frm, list):
            for f in frm:
                if isinstance(f, dict) and "Name" in f and "Entity" in f:
                    aliases[f["Name"]] = f["Entity"]
        e = o.get("Expression")
        if isinstance(e, dict) and isinstance(e.get("SourceRef"), dict) and "Property" in o:
            sr = e["SourceRef"]
            ent = sr.get("Entity") or aliases.get(sr.get("Source"), sr.get("Source"))
            yield ("measure" if parent == "Measure" else "column"), ent, o["Property"]
        if parent == "NativeVisualCalculation" and isinstance(e, str):
            _, names, _ = expr_refs(e)
            for x in names:
                yield "vcalc", None, x
        for k, v in o.items():
            yield from _walk_fields(v, k, aliases)
    elif isinstance(o, list):
        for v in o:
            yield from _walk_fields(v, parent, aliases)


def report_fields(reports_dir: Path) -> list:
    """[(report, location, kind, table, name)] from every PBIR-style JSON file
    (visual.json, page.json, report.json) - projections and filters alike."""
    out = []
    reports_dir = Path(reports_dir)
    for f in sorted(reports_dir.rglob("*.json")):
        report = f.relative_to(reports_dir).parts[0]
        where = f.parent.name if f.name == "visual.json" else f.name
        data = json.loads(f.read_text(encoding="utf-8-sig"))
        for kind, table, name in _walk_fields(data):
            out.append((report, where, kind, table, name))
    return out


def consumer_check(model: Model, fields: list, broken: dict | None = None) -> dict:
    """Every report field must resolve to the right table, and no visual may use a broken measure."""
    broken = broken_measures(model) if broken is None else broken
    failures = []
    for report, visual, kind, table, name in fields:
        if kind == "vcalc":
            m = model.measure(name)
            if m and m in broken:
                failures.append((report, visual, f"visual calculation uses broken [{name}]"))
            continue  # other names in a visual calculation refer to visual fields, not the model
        t = model.table(table or "")
        if t is None:
            failures.append((report, visual, f"table {table} missing (binding {name})"))
        elif kind == "measure":
            m = model.measure(name)
            if m is None:
                failures.append((report, visual, f"[{name}] missing"))
            elif model.measures[m].table != t:
                failures.append((report, visual, f"[{name}] bound to {table}, lives in {model.measures[m].table}"))
            elif m in broken:
                failures.append((report, visual, f"[{name}] broken: {broken[m]}"))
        elif model.column(t, name) is None:
            failures.append((report, visual, f"{table}[{name}] missing"))
    reports = sorted({f[0] for f in fields})
    failed = sorted({f[0] for f in failures})
    return {"reports": len(reports), "passed": len(reports) - len(failed),
            "failures": [list(f) for f in failures]}


def column_usage(model: Model, fields: list, ignore_measures: set = frozenset()) -> dict:
    """(table, col) -> {'measures', 'calculated_columns', 'relationships', 'roles',
    'hierarchies', 'sort_by', 'visuals'} - every kind of reference that pins a column."""
    kinds = ("measures", "calculated_columns", "relationships", "roles", "hierarchies", "sort_by", "visuals",
             "model_objects")
    use = {k: {x: [] for x in kinds} for k in model.columns}

    def add(key, kind, who):
        if key in use:
            use[key][kind].append(who)

    for name, m in model.measures.items():
        if name not in ignore_measures:
            for c in resolve(model, measure_text(m))["columns"]:
                add(c, "measures", name)
    for key, col in model.columns.items():
        if col.expression:
            for c in resolve(model, col.expression, key[0])["columns"]:
                add(c, "calculated_columns", f"{key[0]}[{key[1]}]")
        if col.sort_by and (k := model.column(key[0], col.sort_by)):
            add(k, "sort_by", f"{key[0]}[{key[1]}]")
    for r in model.relationships:
        for t, c in ((r.from_table, r.from_column), (r.to_table, r.to_column)):
            if (k := model.column(t, c)):
                add(k, "relationships", r.name)
    for role, table, expr in model.roles:
        for c in resolve(model, expr, model.table(table))["columns"]:
            add(c, "roles", role)
    for t, c in model.hierarchy_columns:
        if (k := model.column(t, c)):
            add(k, "hierarchies", "hierarchy")
    for why, t, c in model.pinned_columns:                 # OLS, perspectives
        if (k := model.column(t, c)):
            add(k, "model_objects", why)
    for why, expr in model.dax_sources:                    # calc items, calculated tables, field params
        for c in resolve(model, expr)["columns"]:
            add(c, "model_objects", why)
    for table, code in model.m_sources:                    # Power Query: any quoted/bracketed name
        names = {(a or b).lower() for a, b in re.findall(r'\[([^\]]+)\]|"([^"]+)"', code)}
        for (t, c) in model.columns:
            if t == table and c.lower() in names:
                add((t, c), "model_objects", f"M partition {table}")
    for report, visual, kind, table, name in fields:
        t = model.table(table or "")
        if t is None:
            continue
        if kind == "column" and (k := model.column(t, name)):
            add(k, "visuals", f"{report}/{visual}")
    return use


_STATS_CACHE: dict = {}   # (parquet path, mtime_ns, size, column) -> stats


def column_stats(data_dir: Path, model: Model) -> dict:
    """(table, col) -> {rows, distinct, avg_len} from the synthetic parquet data.
    Cached per parquet file version, so repeated analyses of the same data are cheap."""
    stats, con = {}, None
    for table in model.tables:
        f = Path(data_dir) / f"{table}.parquet"
        if not f.exists():
            continue
        st_ = f.stat()
        ver = (str(f.resolve()), st_.st_mtime_ns, st_.st_size)
        wanted = [c for (t, c) in model.columns if t == table]
        missing = [c for c in wanted if ver + (c,) not in _STATS_CACHE]
        if missing:
            con = con or duckdb.connect()
            avail = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM '{f.as_posix()}'").fetchall()}
            cols = [c for c in missing if c in avail]
            for c in missing:
                _STATS_CACHE[ver + (c,)] = None
            if cols:
                parts = ["COUNT(*)"]
                for c in cols:
                    parts += [f'COUNT(DISTINCT "{c}")', f'AVG(length(CAST("{c}" AS VARCHAR)))']
                row = con.execute(f"SELECT {', '.join(parts)} FROM '{f.as_posix()}'").fetchone()
                for i, c in enumerate(cols):
                    _STATS_CACHE[ver + (c,)] = {"rows": row[0], "distinct": row[1 + 2 * i],
                                                "avg_len": row[2 + 2 * i] or 0}
        for c in wanted:
            if _STATS_CACHE.get(ver + (c,)):
                stats[(table, c)] = _STATS_CACHE[ver + (c,)]
    if con:
        con.close()
    return stats


def estimate_bytes(dtype: str, st: dict) -> int:
    """ESTIMATE of in-memory column size (VertiPaq-like): dictionary + bit-packed
    index. Not the real engine size; good enough to rank columns."""
    rows, distinct = st["rows"], max(st["distinct"], 1)
    value_bytes = {"int64": 8, "double": 8, "dateTime": 8, "boolean": 1}.get(dtype, None)
    if value_bytes is None:  # string: per-entry overhead + characters (UTF-16)
        value_bytes = 16 + 2 * st["avg_len"]
    dictionary = distinct * value_bytes
    index = rows * max(1, math.ceil(math.log2(distinct + 1))) / 8
    return int(dictionary + index)


def analyze(model_dir: Path, data_dir: Path, reports_dir: Path) -> dict:
    model = load_model(model_dir)
    return analyze_model(model, report_fields(reports_dir), column_stats(data_dir, model))


def analyze_model(model: Model, fields: list, stats: dict) -> dict:
    broken = broken_measures(model)
    usage = column_usage(model, fields, ignore_measures=set(broken))
    cols = []
    for (t, c), col in model.columns.items():
        st = stats.get((t, c))
        est = estimate_bytes(col.props.get("dataType", "string"), st) if st else None
        u = usage[(t, c)]
        cols.append({"table": t, "column": c, "dataType": col.props.get("dataType"),
                     "distinct": st["distinct"] if st else None, "est_bytes": est,
                     "used": any(u.values()), "usage": u})
    cols.sort(key=lambda r: -(r["est_bytes"] or 0))
    return {
        "model": model.name,
        "tables": len(model.tables),
        "columns": len(model.columns),
        "fact_columns": sum(1 for (t, _) in model.columns if t == "FactClaims"),
        "measures": len(model.measures),
        "broken_measures": broken,
        "unused_columns": [f"{r['table']}[{r['column']}]" for r in cols if not r["used"]],
        "est_total_mb": round(sum(r["est_bytes"] or 0 for r in cols) / 1e6, 3),
        "est_note": "ESTIMATE: dictionary + bit-packed index from synthetic data stats, not engine-measured",
        "column_detail": cols,
        "consumers": consumer_check(model, fields, broken),
        "unsupported": list(model.unsupported),
    }


def normalise(expr: str) -> str:
    """Comparison form of a DAX expression: comments dropped, whitespace outside strings removed."""
    expr = strip_comments(expr or "")
    parts = re.split(r'("(?:[^"]|"")*")', expr)
    return "".join(p if i % 2 else re.sub(r"\s+", "", p) for i, p in enumerate(parts))


def drift(model_dirs: dict) -> dict:
    """Compare tables, columns (+ dataType, calculated expression, sortByColumn),
    measures (+ home table, normalised expression) and relationships across environments."""
    models = {env: load_model(p) for env, p in model_dirs.items()}
    envs = list(models)
    out = {"tables": [], "columns": [], "column_properties": [], "measures": [], "expressions": [],
           "relationships": []}

    def presence(kind, keys_by_env, label):
        for k in sorted(set().union(*keys_by_env.values())):
            have = [e for e in envs if k in keys_by_env[e]]
            if len(have) != len(envs):
                out[kind].append({"object": label(k), "present_in": have})

    presence("tables", {e: set(m.tables) for e, m in models.items()}, str)
    presence("columns", {e: set(m.columns) for e, m in models.items()}, lambda k: f"{k[0]}[{k[1]}]")
    presence("measures", {e: set(m.measures) for e, m in models.items()}, lambda k: f"[{k}]")
    presence("relationships",
             {e: {(r.from_table, r.from_column, r.to_table, r.to_column) for r in m.relationships}
              for e, m in models.items()},
             lambda k: f"{k[0]}.{k[1]} -> {k[2]}.{k[3]}")
    common_cols = set.intersection(*(set(m.columns) for m in models.values()))
    for k in sorted(common_cols):
        sig = {e: (models[e].columns[k].props.get("dataType"), normalise(models[e].columns[k].expression or ""),
                   models[e].columns[k].sort_by) for e in envs}
        if len(set(sig.values())) > 1:
            out["column_properties"].append({"object": f"{k[0]}[{k[1]}]",
                                             "values": {e: list(v) for e, v in sig.items()}})
    common_meas = set.intersection(*(set(m.measures) for m in models.values()))
    for k in sorted(common_meas):
        sig = {e: (models[e].measures[k].table, normalise(models[e].measures[k].expression)) for e in envs}
        if len(set(sig.values())) > 1:
            out["expressions"].append({"object": f"[{k}]", "expressions": {
                e: f"{models[e].measures[k].table}: {models[e].measures[k].expression}" for e in envs}})
    out["drift_count"] = sum(len(v) for v in out.values())
    return out
