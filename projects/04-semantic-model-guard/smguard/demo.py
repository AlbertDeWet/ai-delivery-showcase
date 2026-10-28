"""End-to-end demo: generate -> analyze -> plan -> backup + apply to copy ->
re-analyze -> regression suite -> consumer check -> drift. Writes results/."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import duckdb

from . import generate as gen
from .analyze import analyze, drift
from .regress import classify, run_suite
from .tmdl import load_model
from .trim import apply, plan

REFRESH_ENV = "PROD"      # simulate a data refresh between before and after
REFRESH_ROWS = 2_000
# Human sign-off for UNTESTED measures lives OUTSIDE the code, in a reviewed file:
# results/acknowledgements.json  [{measure, reviewer, date, reason}, ...]
ACK_FILE = Path(__file__).resolve().parent.parent / "results" / "acknowledgements.json"


PLACEHOLDERS = {"tbd", "tba", "placeholder", "na", "none", "x", "xx", "xxx", "reviewer", "todo", "pending",
                "unknown", "someone", "name", "author", "test", "nobody", "anon", "anonymous"}
PLACEHOLDER_PARTS = ("placeholder", "pending", "tbd", "todo")
TOOL_WORDS = {"smguard", "claude", "agent", "bot", "ai", "automated", "automation", "gpt", "chatgpt", "copilot",
              "llm", "assistant", "script", "ci", "pipeline", "system", "robot", "auto"}
TOOL_PARTS = ("smguard", "claude", "chatgpt", "copilot", "openai", "anthropic")
EARLIEST = "2000-01-01"


def canonical_dax(expr: str | None) -> str:
    """Token-canonical DAX: comments dropped and whitespace collapsed BETWEEN tokens only;
    string literals, [bracketed] names and 'quoted' table names are kept byte-exact."""
    from .dax import tokens
    return " ".join(text for _, text in tokens(expr or ""))


def _canonical_measure(m) -> str:
    return "\x1f".join(canonical_dax(x) for x in (m.expression, m.format_expression, m.detail_expression))


def definition_hash(measure) -> str:
    """SHA-256 over everything that determines what the measure computes, transitively:
      * the measure's own canonical definition (expression, format string, detail rows);
      * every measure it depends on;
      * every CALCULATED column it reads, and those columns' own dependencies;
      * conservatively, ALL calculation items of any calculation group whose table it touches.
    Each part is `kind:name=canonical text`, sorted, so order in the files does not matter."""
    import hashlib
    from .analyze import measure_text, resolve
    model = measure.model
    if model is None:
        return hashlib.sha256(f"[{measure.name}]={_canonical_measure(measure)}".encode("utf-8")).hexdigest()
    cg_tables = {t for t, *_ in model.calc_items}
    parts, seen = {}, set()
    stack = [("measure", measure.name)]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        kind, key = node
        if kind == "measure":
            m = model.measures[key]
            parts[f"measure:[{key}]"] = _canonical_measure(m)
            refs = resolve(model, measure_text(m))
        else:                                   # calculated column (table, col)
            col = model.columns[key]
            parts[f"column:{key[0]}[{key[1]}]"] = canonical_dax(col.expression)
            refs = resolve(model, col.expression, key[0])
        for dep in refs["measures"]:
            stack.append(("measure", dep))
        touched = set(refs["tables"]) | {t for t, _ in refs["columns"]}
        for ck in refs["columns"]:
            if model.columns[ck].expression:
                stack.append(("column", ck))
        for t in sorted(touched & cg_tables):
            for tt, item, expr, fmt in model.calc_items:
                if tt == t:
                    parts[f"calcitem:{tt}.{item}"] = canonical_dax(expr) + "\x1f" + canonical_dax(fmt)
    body = "\n".join(f"{k}={v}" for k, v in sorted(parts.items()))
    # the signed measure itself is always first, so renaming it changes the hash
    return hashlib.sha256((f"root:[{measure.name}]\n" + body).encode("utf-8")).hexdigest()


# look-alike characters folded before the tool-name check (Cyrillic, Greek, small capitals, IPA, digits)
CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "ԁ": "d", "ӏ": "l", "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "г": "r",
    "α": "a", "ο": "o", "ν": "v", "ρ": "p", "κ": "k", "ι": "i", "τ": "t", "υ": "u", "ε": "e", "β": "b",
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j",
    "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u",
    "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z", "ɡ": "g", "ɑ": "a", "ı": "i",
    "1": "l", "0": "o", "3": "e", "5": "s", "4": "a", "7": "t", "|": "l",
})


def _reviewer_problem(reviewer: str) -> str | None:
    """Speed bump, NOT authentication: catches placeholders and tool/agent names."""
    import re as _re
    import unicodedata
    norm = unicodedata.normalize("NFKC", reviewer)
    # drop invisible format characters (zero-width, soft hyphen) and combining marks
    norm = "".join(ch for ch in unicodedata.normalize("NFD", norm)
                   if unicodedata.category(ch) not in ("Cf", "Mn", "Me"))
    norm = unicodedata.normalize("NFC", norm).casefold()
    compact = "".join(_re.findall(r"[^\W_]+", norm))
    if compact in PLACEHOLDERS or any(x in compact for x in PLACEHOLDER_PARTS):
        return "placeholder reviewer"
    folded = norm.translate(CONFUSABLES)
    words = set(_re.findall(r"[^\W_]+", folded))
    fcompact = "".join(words) if len(words) == 1 else "".join(_re.findall(r"[^\W_]+", folded))
    if words & TOOL_WORDS or fcompact in TOOL_WORDS:
        return "reviewer is a tool or agent, not a human"
    if len(_re.findall(r"[^\W\d_]", norm)) < 2:
        return "reviewer needs at least 2 letters"
    return None


def signoff_problem(rec) -> str | None:
    """Why a record is NOT a valid human sign-off, or None when it is (record-level checks;
    the definition hash is compared with the live model in review_acknowledgements)."""
    import datetime as dt
    import re as _re
    if not isinstance(rec, dict):
        return "not an object"
    for k in ("measure", "reviewer", "date", "reason"):
        if not str(rec.get(k, "")).strip():
            return f"missing {k}"
    raw = str(rec["date"]).strip()
    try:
        d = dt.datetime.fromisoformat(raw).date() if "T" in raw else dt.date.fromisoformat(raw)
    except ValueError:
        return "date is not an ISO date (YYYY-MM-DD)"
    if d < dt.date.fromisoformat(EARLIEST):
        return f"date before {EARLIEST} is not credible"
    latest_today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=14)).date()   # UTC+14
    if d > latest_today:
        return "date is in the future"
    why = _reviewer_problem(str(rec["reviewer"]))
    if why:
        return why
    if str(rec.get("reviewer_role", "")).strip().casefold() != "human":
        return "reviewer_role must be 'human'"
    if not _re.fullmatch(r"[0-9a-f]{64}", str(rec.get("definition_sha256", "")).strip().lower()):
        return "missing definition_sha256 (the sign-off must name the exact definition it reviewed)"
    return None


def review_acknowledgements(path: Path = ACK_FILE, model=None) -> dict:
    """measure -> {**record, accepted, rejected_because} for every record in the file.
    With `model`, the record's definition_sha256 must match the measure's CURRENT definition."""
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = items if isinstance(items, list) else items.get("acknowledgements", [])
    out = {}
    for i in items:
        if isinstance(i, dict) and str(i.get("measure", "")).strip():
            why = signoff_problem(i)
            if why is None and model is not None:
                m = model.measure(i["measure"])
                if m is None:
                    why = "measure not in this model"
                elif definition_hash(model.measures[m]) != str(i["definition_sha256"]).strip().lower():
                    why = "definition changed since the sign-off (hash mismatch)"
            out[i["measure"]] = {**i, "accepted": why is None, "rejected_because": why}
    return out


def load_acknowledgements(path: Path = ACK_FILE, model=None) -> dict:
    """measure -> record, VALID human sign-offs only (placeholders, tools, bad dates, stale hash rejected)."""
    return {m: r for m, r in review_acknowledgements(path, model).items() if r["accepted"]}


def run(root: Path) -> dict:
    root = Path(root)
    gen.generate(root)
    models, data, reports, work, res = (root / "models", root / "data", root / "reports",
                                        root / "work", root / "results")
    res.mkdir(exist_ok=True)
    import shutil
    shutil.rmtree(work, ignore_errors=True)  # demo-owned scratch folder, rebuilt every run
    summary = {"envs": {}, "note": "Sizes are ESTIMATES from synthetic data stats, not engine-measured."}
    con = duckdb.connect()
    for env in gen.ENVS:
        # sign-offs are checked against THIS environment's current measure definitions
        reviewed = review_acknowledgements(model=load_model(models / env))
        acks = {m: r for m, r in reviewed.items() if r["accepted"]}
        p = plan(models / env, reports)
        (res / f"plan_{env}.json").write_text(json.dumps(p, indent=2), encoding="utf-8")
        out = apply(models / env, data / env / "v1", reports, work / env, p)
        before_data = data / env / "v1"
        after_data = before_data
        if env == REFRESH_ENV:
            after_data = data / env / "v2"
            gen.write_data(con, env, after_data, extra_rows=REFRESH_ROWS)
        before = run_suite(models / env, before_data)
        after_on_old = run_suite(out["trimmed"], before_data)
        after = run_suite(out["trimmed"], after_data)
        rows = classify(before, after_on_old, after)
        with open(res / f"regression_{env}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["check", "class", "reason"])
            w.writeheader()
            w.writerows(rows)
        b, a = out["before"], out["after"]
        untested = sorted({r["check"].split("::", 1)[1] for r in rows if r["class"] == "UNTESTED"})
        counts = {}
        for r in rows:
            key = r["class"] if r["class"] != "EXPECTED" else "EXPECTED: " + r["reason"].split(":")[0]
            counts[key] = counts.get(key, 0) + 1
        summary["envs"][env] = {
            "est_mb_before": b["est_total_mb"], "est_mb_after": a["est_total_mb"],
            "tables_before": b["tables"], "tables_after": a["tables"],
            "columns_before": b["columns"], "columns_after": a["columns"],
            "fact_columns_before": b["fact_columns"], "fact_columns_after": a["fact_columns"],
            "measures_before": b["measures"], "measures_after": a["measures"],
            "broken_before": len(b["broken_measures"]), "broken_after": len(a["broken_measures"]),
            "removed_columns": len(p["remove_columns"]), "removed_measures": len(p["remove_measures"]),
            "blocked_measures": len(p["blocked_measures"]),
            "consumers_before": f"{b['consumers']['passed']}/{b['consumers']['reports']}",
            "consumers_after": f"{a['consumers']['passed']}/{a['consumers']['reports']}",
            "regression_checks": len(rows), "regression": counts,
            "unexpected": sum(1 for r in rows if r["class"] == "UNEXPECTED"),
            "untested": sum(1 for r in rows if r["class"] == "UNTESTED"),
            "untested_measures": untested,
            # every sign-off RECORD for an untested measure, with whether it is accepted
            "untested_acknowledged": {m: reviewed[m] for m in untested if m in reviewed},
            "untested_signed_off": [m for m in untested if m in acks],
            "safety_checks": out["checks"], "trim_safe": out["safe"],
            "safe": out["safe"] and not any(r["class"] == "UNEXPECTED" for r in rows)
                    and all(m in acks for m in untested),
            "status": _status(out["safe"], rows, untested, acks),
            "refreshed_data_rows": REFRESH_ROWS if env == REFRESH_ENV else 0,
        }
        detail = {"before": {k: v for k, v in b.items() if k != "column_detail"},
                  "after": {k: v for k, v in a.items() if k != "column_detail"},
                  "top_columns_before": b["column_detail"][:10]}
        (res / f"analysis_{env}.json").write_text(json.dumps(detail, indent=2, default=str), encoding="utf-8")
    con.close()
    d_before = drift({e: models / e for e in gen.ENVS})
    d_after = drift({e: work / e / "trimmed" for e in gen.ENVS})
    (res / "drift.json").write_text(json.dumps({"before": d_before, "after": d_after}, indent=2), encoding="utf-8")
    summary["drift_before"] = d_before["drift_count"]
    summary["drift_after"] = d_after["drift_count"]
    summary["drift_after_items"] = [x["object"] for k in ("tables", "columns", "measures", "expressions")
                                    for x in d_after[k]]
    (res / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (res / "summary.md").write_text(_markdown(summary), encoding="utf-8")
    return summary


def _status(trim_safe: bool, rows: list, untested: list, acks: dict) -> str:
    unexpected = sum(1 for r in rows if r["class"] == "UNEXPECTED")
    if not trim_safe or unexpected:
        return f"blocked: {unexpected} unexpected" + ("" if trim_safe else ", trim safety checks failed")
    pending = [m for m in untested if m not in acks]
    if pending:
        n = sum(1 for r in rows if r["class"] == "UNTESTED")
        return f"pending human sign-off ({n} untested)"
    return "safe"


def _markdown(s: dict) -> str:
    L = ["# Demo results (synthetic Contoso Insurance models)", "",
         "Sizes are ESTIMATES (dictionary + bit-packed index from synthetic data stats).", "",
         "| Env | Est. MB before | Est. MB after | Fact cols before -> after | Columns | Measures | Broken | Reports passing | Regression checks | Unexpected | Untested | Status |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for env, e in s["envs"].items():
        L.append(f"| {env} | {e['est_mb_before']} | {e['est_mb_after']} | {e['fact_columns_before']} -> {e['fact_columns_after']} | "
                 f"{e['columns_before']} -> {e['columns_after']} | {e['measures_before']} -> {e['measures_after']} | "
                 f"{e['broken_before']} -> {e['broken_after']} | {e['consumers_after']} | {e['regression_checks']} | {e['unexpected']} | {e['untested']} | {e['status']} |")
    L += ["", "Regression classification per env:", ""]
    for env, e in s["envs"].items():
        L.append(f"- {env}: " + ", ".join(f"{k} = {v}" for k, v in sorted(e["regression"].items())))
    L += ["", f"Drift items across DEV/UAT/PROD: before {s['drift_before']}, after {s['drift_after']} "
          f"({', '.join(s['drift_after_items']) or 'none'})", ""]
    return "\n".join(L)
