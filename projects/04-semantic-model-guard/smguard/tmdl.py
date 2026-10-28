"""TMDL (Tabular Model Definition Language) reader and surgical remover - STRICT ALLOWLIST.

Every file in the model folder must be an allowlisted file type, every object must be an
allowlisted child of its parent, and every property must be allowlisted for its object.
Anything else is recorded in `Model.unsupported`, and trim refuses while that list is
non-empty. See SUPPORTED_* below and the README table "Supported subset".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- the allowlist
SUPPORTED_FILES = {           # path pattern (relative to the model folder) -> allowed root objects
    "definition/database.tmdl": {"database"},
    "definition/model.tmdl": {"model", "queryGroup", "annotation", "ref"},
    "definition/cultures/*.tmdl": {"cultureInfo"},
    "definition/tables/*.tmdl": {"table"},
    "definition/relationships.tmdl": {"relationship"},
    "definition/roles/*.tmdl": {"role"},
    "definition/perspectives/*.tmdl": {"perspective"},
}
# PBIP metadata next to definition/ (per the public PBIP folder docs): read-only, carry no
# model references, ignored. `.pbi/` holds Desktop editor/local settings and the local cache.
METADATA_FILES = {".platform", "definition.pbism", "diagramLayout.json",
                  ".pbi/editorSettings.json", ".pbi/localSettings.json", ".pbi/cache.abf"}
REF_KINDS = {"table", "culture", "role", "perspective"}   # `ref <kind> <name>` in model.tmdl
CHILDREN = {
    None: set(),  # filled per file from SUPPORTED_FILES
    "table": {"column", "measure", "hierarchy", "partition", "calculationGroup", "annotation"},
    "column": {"annotation"},
    "measure": {"annotation"},
    "hierarchy": {"level", "annotation"},
    "level": {"annotation"},
    "partition": {"annotation"},
    "calculationGroup": {"calculationItem"},
    "calculationItem": set(),
    "relationship": {"annotation"},
    "role": {"tablePermission", "annotation"},
    "tablePermission": {"columnPermission"},
    "columnPermission": set(),
    "perspective": {"perspectiveTable", "annotation"},
    "perspectiveTable": {"perspectiveColumn", "perspectiveMeasure", "perspectiveHierarchy"},
    "perspectiveColumn": set(), "perspectiveMeasure": set(), "perspectiveHierarchy": set(),
    "annotation": set(),
    "database": set(),
    "model": {"dataAccessOptions", "annotation"},
    "dataAccessOptions": set(),
    "queryGroup": {"annotation"},
    "ref": set(),
    "cultureInfo": {"linguisticMetadata"},
    "linguisticMetadata": set(),
}
PROPS = {
    "table": {"lineageTag", "isHidden", "dataCategory", "description"},
    "column": {"dataType", "sourceColumn", "sortByColumn", "formatString", "summarizeBy", "isHidden", "lineageTag",
               "dataCategory", "displayFolder", "isKey", "isNullable", "isNameInferred", "isDataTypeInferred",
               "isAvailableInMdx", "description", "sourceLineageTag"},
    "measure": {"formatString", "displayFolder", "lineageTag", "isHidden", "description"},
    "hierarchy": {"lineageTag", "isHidden", "displayFolder", "description"},
    "level": {"column", "lineageTag", "ordinal"},
    "partition": {"mode", "dataView", "queryGroup"},
    "calculationGroup": {"precedence", "description"},
    "calculationItem": {"ordinal"},
    "relationship": {"fromColumn", "toColumn", "crossFilteringBehavior", "isActive", "fromCardinality",
                     "toCardinality", "securityFilteringBehavior", "relyOnReferentialIntegrity", "joinOnDateBehavior"},
    "role": {"modelPermission", "description"},
    "database": {"compatibilityLevel", "compatibilityMode", "language", "collation"},
    "model": {"culture", "defaultPowerBIDataSourceVersion", "sourceQueryCulture", "discourageImplicitMeasures",
              "defaultMode", "description"},
    "dataAccessOptions": {"legacyRedirects", "returnErrorValuesAsNull", "fastCombine"},
    "queryGroup": {"description"},
    "linguisticMetadata": {"contentType"},
}
EXPR_PROPS = {   # expression-valued properties, all read as reference sources
    "measure": {"formatStringDefinition", "detailRowsDefinition"},
    "partition": {"source"},
    "calculationItem": {"formatStringDefinition"},
}
HEADER_EXPR = {"measure", "column", "calculationItem", "partition", "tablePermission", "columnPermission",
               "annotation", "linguisticMetadata"}
BARE_OBJECTS = {"calculationGroup", "dataAccessOptions", "linguisticMetadata"}   # objects without a name
OPTIONAL_NAME = {"database", "model"}
KEYWORDS = sorted({k for k in CHILDREN if k and k != "ref"} | {"culture", "expression", "kpi", "function",
                                                 "changedProperty", "extendedProperty", "dataSource"})


@dataclass
class Column:
    table: str
    name: str
    props: dict = field(default_factory=dict)
    expression: str | None = None     # calculated column

    @property
    def sort_by(self):
        v = self.props.get("sortByColumn")
        return unquote(v) if v else None


@dataclass
class Measure:
    table: str
    name: str
    expression: str
    format_expression: str | None = None   # formatStringDefinition
    detail_expression: str | None = None   # detailRowsDefinition
    model: "Model | None" = field(default=None, repr=False, compare=False)   # back-reference


@dataclass
class Relationship:
    name: str
    from_table: str
    from_column: str
    to_table: str
    to_column: str


@dataclass
class Model:
    name: str
    path: Path | None
    columns: dict = field(default_factory=dict)
    measures: dict = field(default_factory=dict)
    relationships: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    hierarchies: dict = field(default_factory=dict)        # (table, hierarchy) -> [columns]
    hierarchy_columns: set = field(default_factory=set)
    roles: list = field(default_factory=list)              # (role, table, filter DAX)
    pinned_columns: list = field(default_factory=list)     # (why, table, col)
    perspective_measures: set = field(default_factory=set)
    dax_sources: list = field(default_factory=list)        # (why, DAX)
    m_sources: list = field(default_factory=list)          # (table, M code)
    unsupported: list = field(default_factory=list)
    refs: list = field(default_factory=list)               # (kind, name, file) from model.tmdl
    role_names: list = field(default_factory=list)
    calc_items: list = field(default_factory=list)         # (table, item, DAX, format DAX)
    culture_names: list = field(default_factory=list)

    def table(self, name: str):
        n = name.lower()
        return next((t for t in self.tables if t.lower() == n), None)

    def column(self, table: str, col: str):
        t, c = table.lower(), col.lower()
        return next((k for k in self.columns if k[0].lower() == t and k[1].lower() == c), None)

    def measure(self, name: str):
        n = name.lower()
        return next((m for m in self.measures if m.lower() == n), None)


def unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
        return name[1:-1].replace("''", "'")
    return name


def quote(name: str) -> str:
    return f"'{name}'" if re.search(r"[^A-Za-z0-9_]", name) else name


_NAME = r"('(?:[^']|'')*'|[^\s=']+)"
_REF = re.compile(r"^ref\s+([A-Za-z]+)\s+" + _NAME + r"\s*$")
_OBJ = re.compile(r"^(" + "|".join(KEYWORDS) + r")(?:\s+" + _NAME + r")?\s*(?:=\s*(.*))?$")
_PROP = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\s*:(?:\s(.*)|)$")
_FLAG = re.compile(r"^([A-Za-z][A-Za-z0-9]*)$")
_EXPR_PROP = re.compile(r"^([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*)$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip("\t"))


def _clean(line: str, first: bool) -> str:
    line = line.rstrip("\r\n")
    return line.lstrip("﻿") if first else line


def _read_body(lines, i, depth, rhs):
    """Expression whose header/property sits at `depth` (+1 for properties is passed in)."""
    n = len(lines)
    rhs = rhs.strip()
    if rhs.startswith("```"):
        body = [rhs[3:]] if rhs[3:].strip() else []
        while i < n and lines[i].strip() != "```":
            body.append(lines[i].rstrip("\r\n").strip())
            i += 1
        return "\n".join(body).strip(), i + 1
    body = [rhs] if rhs else []
    while i < n:
        r = lines[i].rstrip("\r\n")
        if not r.strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n and _indent(lines[j].rstrip("\r\n")) > depth + 1:
                i = j
                continue
            break
        if _indent(r) <= depth + 1:
            break
        body.append(r.strip())
        i += 1
    return "\n".join(body).strip(), i


def _blocks(text: str, root_kw=None, unknown: list | None = None):
    """Yield dicts {depth, kw, name, expr, props, xprops, parent, start, end} for every
    object, enforcing the allowlist (violations are appended to `unknown`)."""
    def bad(i, s, why):
        if unknown is not None:
            unknown.append(f"line {i + 1}: {why}: {s[:80]}")

    lines = text.splitlines(keepends=True)
    i, n, stack = 0, len(lines), []
    while i < n:
        raw = _clean(lines[i], i == 0)
        s = raw.strip()
        if not s or s.startswith("//"):
            i += 1
            continue
        if raw.lstrip("\t") != raw.lstrip("\t").lstrip(" ") or (raw[:1] == " "):
            bad(i, s, "space indentation")
        depth = _indent(raw)
        rm = _REF.match(s)
        m = _OBJ.match(s)
        if rm:
            kw, name, rhs = "ref", unquote(rm.group(2)), None
            ref_kind = rm.group(1)
        elif m:
            kw, name, rhs = m.group(1), unquote(m.group(2) or ""), m.group(3)
            ref_kind = None
        else:
            bad(i, s, "not an allowlisted object")
            i += 1
            continue
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else None
        roots = root_kw if isinstance(root_kw, (set, frozenset)) else {root_kw}
        allowed = roots if parent is None else CHILDREN.get(parent, set())
        if kw not in allowed or (parent is None and depth != 0) or (stack and depth != stack[-1][0] + 1):
            bad(i, s, f"object '{kw}' not allowed under '{parent}'")
        if kw in BARE_OBJECTS and name or (kw not in BARE_OBJECTS | OPTIONAL_NAME and not name):
            bad(i, s, "object name")
        if kw == "ref" and ref_kind not in REF_KINDS:
            bad(i, s, f"ref {ref_kind} not supported")
        if rhs is not None and kw not in HEADER_EXPR:
            bad(i, s, f"'{kw}' cannot have an expression")
        stack.append((depth, kw))
        start = i
        while start > 0 and lines[start - 1].strip().startswith("///") and \
                _indent(_clean(lines[start - 1], start - 1 == 0)) == depth:
            start -= 1
        i += 1
        expr = None
        if rhs is not None:
            expr, i = _read_body(lines, i, depth, rhs)
        props, xprops = {}, {}
        while i < n:
            r = _clean(lines[i], i == 0)
            st = r.strip()
            if not st or st.startswith("//"):
                i += 1
                continue
            d = _indent(r)
            if d <= depth or _OBJ.match(st):
                break
            if r.lstrip("\t")[:1] == " ":
                bad(i, st, "space indentation")
                i += 1
                continue
            if d != depth + 1:
                bad(i, st, "unexpected nesting")
                i += 1
                continue
            if (pm := _PROP.match(st)):
                if pm.group(1) not in PROPS.get(kw, set()):
                    bad(i, st, f"property not allowlisted for '{kw}'")
                props[pm.group(1)] = (pm.group(2) or "").strip()
                i += 1
            elif (em := _EXPR_PROP.match(st)):
                if em.group(1) not in EXPR_PROPS.get(kw, set()):
                    bad(i, st, f"expression property not allowlisted for '{kw}'")
                xprops[em.group(1)], i = _read_body(lines, i + 1, depth + 1, em.group(2))
                if not xprops[em.group(1)]:
                    bad(i, st, "empty or mis-indented expression")
            elif (fm := _FLAG.match(st)) and fm.group(1) in PROPS.get(kw, set()):
                props[fm.group(1)] = "true"
                i += 1
            else:
                bad(i, st, f"not allowlisted inside '{kw}'")
                i += 1
        end = i
        while end < n:
            r = lines[end].rstrip("\r\n")
            if r.strip() and _indent(r) <= depth:
                break
            end += 1
        while end > start + 1 and lines[end - 1].strip().startswith("///"):
            end -= 1
        if kw == "ref":
            props["kind"] = ref_kind
        yield {"depth": depth, "kw": kw, "name": name, "expr": expr, "props": props, "xprops": xprops,
               "parent": parent, "start": start, "end": end}


def parse_table_file(text: str, model: Model, where: str = "") -> None:
    table, hierarchy, unknown = None, None, []
    for b in _blocks(text, "table", unknown):
        kw, name, expr, props, xp = b["kw"], b["name"], b["expr"], b["props"], b["xprops"]
        if kw == "table":
            table = name
            model.tables.append(name)
        elif table is None:
            continue
        elif kw == "measure":
            if model.measure(name):
                unknown.append(f"duplicate measure {name}")
            model.measures[name] = Measure(table, name, expr or "", xp.get("formatStringDefinition"),
                                           xp.get("detailRowsDefinition"))
        elif kw == "column":
            model.columns[(table, name)] = Column(table, name, props, expr)
        elif kw == "hierarchy":
            hierarchy = name
            model.hierarchies[(table, name)] = []
        elif kw == "level":
            if hierarchy and "column" in props:
                c = unquote(props["column"])
                model.hierarchy_columns.add((table, c))
                model.hierarchies[(table, hierarchy)].append(c)
        elif kw == "calculationItem":
            model.dax_sources.append((f"calculationItem {table}.{name}", expr or ""))
            model.calc_items.append((table, name, expr or "", xp.get("formatStringDefinition")))
            if "formatStringDefinition" in xp:
                model.dax_sources.append((f"calculationItem {table}.{name} format", xp["formatStringDefinition"]))
        elif kw == "partition":
            kind = (expr or "").strip().lower()
            src = xp.get("source", "")
            if kind == "calculated":
                model.dax_sources.append((f"calculated partition {table}", src))
            elif kind == "m":
                model.m_sources.append((table, src))
            else:
                unknown.append(f"partition kind '{expr}' not supported")
    model.unsupported += [f"{where}: {u}" for u in unknown]


def parse_role_file(text: str, model: Model, where: str = "") -> None:
    role, perm_table, unknown = None, None, []
    for b in _blocks(text, "role", unknown):
        if b["kw"] == "role":
            role = b["name"]
            model.role_names.append(role)
        elif b["kw"] == "tablePermission":
            perm_table = b["name"]
            if b["expr"]:
                model.roles.append((role, b["name"], b["expr"]))
        elif b["kw"] == "columnPermission" and perm_table:
            model.pinned_columns.append((f"OLS {role}", perm_table, b["name"]))
    model.unsupported += [f"{where}: {u}" for u in unknown]


def parse_perspective_file(text: str, model: Model, where: str = "") -> None:
    persp, ptable, unknown, hier = None, None, [], []
    for b in _blocks(text, "perspective", unknown):
        kw, name = b["kw"], b["name"]
        if kw == "perspective":
            persp = name
        elif kw == "perspectiveTable":
            ptable = name
        elif kw == "perspectiveColumn":
            model.pinned_columns.append((f"perspective {persp}", ptable, name))
        elif kw == "perspectiveMeasure":
            model.perspective_measures.add(name)
        elif kw == "perspectiveHierarchy":
            hier.append((ptable, name))
    for t, h in hier:   # resolved after all tables are known (see load_model)
        model.pinned_columns.append((f"perspective {persp} hierarchy {h}", t, f"\0hierarchy:{h}"))
    model.unsupported += [f"{where}: {u}" for u in unknown]


def _ref(text: str) -> tuple:
    m = re.match(r"^('(?:[^']|'')*'|[^.]+)\.('(?:[^']|'')*'|.+)$", text.strip())
    if not m:
        raise ValueError(f"bad column ref {text!r}")
    return unquote(m.group(1)), unquote(m.group(2))


def parse_relationships(text: str, model: Model, where: str = "") -> None:
    unknown = []
    for b in _blocks(text, "relationship", unknown):
        if b["kw"] == "relationship":
            p = b["props"]
            if "fromColumn" in p and "toColumn" in p:
                ft, fc = _ref(p["fromColumn"])
                tt, tc = _ref(p["toColumn"])
                model.relationships.append(Relationship(b["name"], ft, fc, tt, tc))
            else:
                unknown.append(f"relationship {b['name']} without fromColumn/toColumn")
    model.unsupported += [f"{where}: {u}" for u in unknown]


def parse_simple_file(text: str, model: Model, where: str, roots: set) -> None:
    """database.tmdl / model.tmdl / cultures: settings only, plus `ref` lists that must match."""
    unknown = []
    for b in _blocks(text, roots, unknown):
        if b["kw"] == "ref":
            model.refs.append((b["props"]["kind"], b["name"], where))
        elif b["kw"] == "cultureInfo":
            model.culture_names.append(b["name"])
    model.unsupported += [f"{where}: {u}" for u in unknown]


def _read(f: Path) -> str:
    return f.read_bytes().decode("utf-8-sig")


def load_model(path: Path) -> Model:
    path = Path(path)
    model = Model(name=path.name, path=path)
    readers = {"definition/tables/*.tmdl": parse_table_file,
               "definition/relationships.tmdl": parse_relationships,
               "definition/roles/*.tmdl": parse_role_file,
               "definition/perspectives/*.tmdl": parse_perspective_file}
    handled = set()
    for pattern, roots in SUPPORTED_FILES.items():
        for f in sorted(path.glob(pattern)):
            where = f.relative_to(path).as_posix()
            if pattern in readers:
                readers[pattern](_read(f), model, where)
            else:
                parse_simple_file(_read(f), model, where, roots)
            handled.add(f.resolve())
    for f in sorted(path.rglob("*")):
        if not f.is_file() or f.resolve() in handled:
            continue
        rel = f.relative_to(path).as_posix()
        if rel in METADATA_FILES:
            continue
        model.unsupported.append(f"{rel}: file not in the supported subset")
    # every `ref` in model.tmdl must point at an object that exists
    existing = {"table": {t.lower() for t in model.tables},
                "role": {r.lower() for r in model.role_names},
                "culture": {c.lower() for c in model.culture_names},
                "perspective": {f.stem.lower() for f in path.glob("definition/perspectives/*.tmdl")}}
    seen = set()
    for kind, name, where in model.refs:
        if name.lower() not in existing.get(kind, set()):
            model.unsupported.append(f"{where}: ref {kind} {name} points at nothing")
        if (kind, name.lower()) in seen:
            model.unsupported.append(f"{where}: duplicate ref {kind} {name}")
        seen.add((kind, name.lower()))
    if (path / "definition" / "model.tmdl").is_file():
        # model.tmdl lists every table: a table file nobody refs is an inconsistent model
        for kind, names in (("table", model.tables), ("role", model.role_names), ("culture", model.culture_names)):
            for x in names:
                if (kind, x.lower()) not in seen:
                    model.unsupported.append(f"definition/model.tmdl: {kind} {x} has a file but no `ref {kind}`")
    for m in model.measures.values():
        m.model = model
    # perspective hierarchies pin their level columns
    pins = []
    for why, t, c in model.pinned_columns:
        if c.startswith("\0hierarchy:"):
            h = c.split(":", 1)[1]
            key = next((k for k in model.hierarchies if k[0].lower() == (t or "").lower() and k[1].lower() == h.lower()), None)
            if key is None:
                model.unsupported.append(f"{why}: hierarchy not found")
            else:
                pins += [(why, key[0], lc) for lc in model.hierarchies[key]]
        else:
            pins.append((why, t, c))
    model.pinned_columns = pins
    return model


def remove_objects(path: Path, columns: set, measures: set) -> list:
    """Remove column/measure blocks (with their `///` doc comments) in place. Files with
    nothing to remove are not rewritten; edited files keep their bytes (BOM, line endings)."""
    changed = []
    for f in sorted((Path(path) / "definition" / "tables").glob("*.tmdl")):
        text = f.read_bytes().decode("utf-8")
        lines = text.splitlines(keepends=True)
        drop, table = set(), None
        for b in _blocks(text, "table"):
            if b["kw"] == "table" and b["depth"] == 0:
                table = b["name"]
            elif b["depth"] == 1 and ((b["kw"] == "measure" and b["name"] in measures) or
                                      (b["kw"] == "column" and (table, b["name"]) in columns)):
                drop.update(range(b["start"], b["end"]))
        if drop:
            if 0 in drop:
                raise ValueError(f"refusing to remove the first line of {f}")
            f.write_bytes("".join(l for i, l in enumerate(lines) if i not in drop).encode("utf-8"))
            changed.append(f)
    return changed
