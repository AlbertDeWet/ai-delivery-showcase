"""Eval runner and markdown report."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from nl2sql import checker, config, executor, grader, guard, pipeline
from nl2sql.schema import SCHEMA_CARD

TIER_ROWS = [("claude-cli", t, m) for t, m in config.TIERS.items()]
RETRY_RULE = ("infra-only: retry once (max 1) when a call errors with 0 tokens consumed "
              "(CLI timeout, process or transport failure); never on a wrong, refused or "
              "unparseable answer; same rule for every provider and tier")
COST_LABEL = "API-equivalent cost, generation calls only (informational; run on subscription)"
QUANTILE_METHOD = "nearest-rank on the sorted list (index = round(q * (n - 1)))"


def load_cases(path: Path = config.EVAL_FILE) -> list[dict]:
    cases = yaml.safe_load(Path(path).read_text(encoding="utf-8"))["cases"]
    for c in cases:
        exp = c["expect"]
        c["expect"] = [exp] if isinstance(exp, str) else list(exp)
    return cases


def grade_case(case: dict, res: pipeline.AskResult, gold_rows, gold_cols=None) -> tuple[bool, str]:
    exp = case["expect"]
    if res.action == "sql":
        if "answer" not in exp:
            return False, "answered but should have " + "/".join(exp)
        ok = grader.result_sets_match(gold_rows, res.rows, gold_cols, res.columns)
        return ok, "result set matches gold" if ok else "result set differs from gold"
    if res.action in ("refuse", "blocked"):
        if "refuse" in exp:
            return True, "refused" if res.action == "refuse" else "blocked by guard"
        return False, f"{res.action} but expected " + "/".join(exp)
    if res.action == "clarify":
        if "clarify" in exp:
            return True, "asked to clarify"
        return False, "asked to clarify but expected " + "/".join(exp)
    return False, f"error: {res.error[:200]}"


def _pct(n, d):
    return None if d == 0 else round(100.0 * n / d, 1)


def _quantile(xs, q):
    """Nearest-rank quantile: the element at index round(q * (n - 1)) of the sorted list."""
    if not xs:
        return None
    xs = sorted(xs)
    idx = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return round(xs[idx], 3)


def run_eval(provider, repeats: int = 1, cases: list[dict] | None = None, db_path=config.DB_PATH,
             run_checker: bool = True, progress=None, suite: str = "questions",
             retry_infra: bool = True, label: str | None = None) -> dict:
    cases = cases if cases is not None else load_cases()
    con = executor.connect(db_path)
    gold_cache = {}
    records = []
    try:
        for c in cases:
            if "answer" in c["expect"] and c.get("gold_sql"):
                gold_cache[c["id"]] = executor.run(c["gold_sql"], con)
        for rep in range(repeats):
            for c in cases:
                res = pipeline.ask(c["question"], provider, con, SCHEMA_CARD, retry_infra=retry_infra)
                gcols, grows = gold_cache.get(c["id"], ([], []))
                ok, why = grade_case(c, res, grows, gcols)
                chk = (checker.check(res, provider, con, retry_infra=retry_infra) if run_checker
                       else checker.CheckResult("abstain"))
                records.append({
                    "id": c["id"], "category": c["category"], "repeat": rep, "expect": c["expect"],
                    "question": c["question"], "action": res.action, "correct": ok, "reason": why,
                    "sql": res.sql, "guard_reasons": res.guard_reasons, "error": res.error,
                    "latency_s": round(res.latency_s, 4), "input_tokens": res.input_tokens,
                    "output_tokens": res.output_tokens, "cost_usd": res.cost_usd,
                    "retries": res.retries, "first_error": res.first_error,
                    "check": {"status": chk.status, "key_number": chk.key_number, "control": chk.control,
                              "note": chk.note, "input_tokens": chk.input_tokens,
                              "output_tokens": chk.output_tokens},
                })
                if progress:
                    progress(records[-1])
    finally:
        con.close()
    return {
        "provider": provider.name,
        "suite": suite,
        "model": provider.model,
        "tier": getattr(provider, "tier", None),
        "repeats": repeats,
        "n_cases": len(cases),
        "case_ids": [c["id"] for c in cases],
        "label": label,
        "retry_rule": RETRY_RULE if retry_infra else "none",
        "grader_version": grader.GRADER_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summarize(records, repeats),
        "records": records,
    }


def summarize(records: list[dict], repeats: int) -> dict:
    n = len(records)
    by_cat = defaultdict(lambda: [0, 0])
    for r in records:
        by_cat[r["category"]][0] += r["correct"]
        by_cat[r["category"]][1] += 1
    answerable = [r for r in records if r["expect"] == ["answer"]]
    non_answer = [r for r in records if "answer" not in r["expect"]]
    destructive = [r for r in records if r["category"] == "adversarial_destructive"]
    false_refusals = [r for r in answerable if r["action"] in ("refuse", "clarify", "blocked")]
    per_case = defaultdict(list)
    for r in records:
        per_case[r["id"]].append(r["correct"])
    lat = [r["latency_s"] for r in records]
    in_tok = sum(r["input_tokens"] + r["check"]["input_tokens"] for r in records)
    out_tok = sum(r["output_tokens"] + r["check"]["output_tokens"] for r in records)
    costs = [r["cost_usd"] for r in records if r["cost_usd"] is not None]

    answered = [r for r in records if r["action"] == "sql"]
    wrong = [r for r in answered if not r["correct"]]
    right = [r for r in answered if r["correct"]]
    return {
        "n_records": n,
        "overall_accuracy_pct": _pct(sum(r["correct"] for r in records), n),
        "execution_accuracy_pct": _pct(sum(r["correct"] for r in answerable), len(answerable)),
        "refusal_correctness_pct": _pct(sum(r["correct"] for r in non_answer), len(non_answer)),
        "false_refusal_pct": _pct(len(false_refusals), len(answerable)),
        "destructive_blocked_pct": _pct(sum(r["action"] in ("refuse", "blocked") for r in destructive),
                                        len(destructive)),
        f"pass^{repeats}_pct": _pct(sum(all(v) for v in per_case.values()), len(per_case)),
        "by_category": {k: {"correct": v[0], "n": v[1], "accuracy_pct": _pct(v[0], v[1])}
                        for k, v in sorted(by_cat.items())},
        "latency_p50_s": _quantile(lat, 0.5),
        "latency_p95_s": _quantile(lat, 0.95),
        "tokens_in": in_tok,
        "tokens_out": out_tok,
        "tokens_per_question": round((in_tok + out_tok) / n, 1) if n else 0,
        "api_equivalent_cost_usd": round(sum(costs), 4) if costs else None,
        "errors": sum(r["action"] == "error" for r in records),
        "infra_retries": sum(r.get("retries", 0) for r in records),
        "checker": {
            "answered": len(answered),
            "wrong_answers": len(wrong),
            "wrong_caught": sum(r["check"]["status"] == "disagree" for r in wrong),
            "wrong_abstained": sum(r["check"]["status"] == "abstain" for r in wrong),
            "caught_rate_pct": _pct(sum(r["check"]["status"] == "disagree" for r in wrong), len(wrong)),
            "correct_answers": len(right),
            "false_alarms": sum(r["check"]["status"] == "disagree" for r in right),
            "agree_on_correct": sum(r["check"]["status"] == "agree" for r in right),
        },
    }


RERUNS_DIR = "reruns"
DERIVED_DIR = "derived"
SUITES = ("questions", "heldout")


def rerun_group(result: dict) -> str:
    """Identity of a labelled re-run series: provider, tier/model, suite and label."""
    tag = result["tier"] or result["model"]
    suite = result.get("suite", "questions")
    suffix = "" if suite == "questions" else f"-{suite}"
    return f"{result['provider']}-{tag}{suffix}-{result.get('label')}"


def result_path(result: dict, results_dir: Path = config.RESULTS_DIR) -> Path:
    """Full runs go to results/. Labelled targeted re-runs go to results/reruns/ as numbered
    attempts (<group>-attemptN.json), so a re-run can never overwrite an earlier attempt."""
    tag = result["tier"] or result["model"]
    suite = result.get("suite", "questions")
    suffix = "" if suite == "questions" else f"-{suite}"
    if result.get("label"):
        n = result.get("attempt")
        name = rerun_group(result) + (f"-attempt{n}" if n else "")
        return Path(results_dir) / RERUNS_DIR / f"{name}.json"
    return Path(results_dir) / f"{result['provider']}-{tag}{suffix}.json"


def rerun_attempts(folder: Path, provider: str, tier, suite: str, label) -> list[tuple[str, dict]]:
    """Every stored attempt of one re-run series in folder (matched by content, so files written
    before attempts were numbered count too). Sorted by attempt number, then timestamp."""
    out = []
    for name, r in _load_runs(Path(folder)):
        if (r.get("provider"), r.get("tier"), r.get("suite", "questions"), r.get("label")) == \
                (provider, tier, suite, label):
            out.append((name, r))
    return sorted(out, key=lambda x: (x[1].get("attempt") or 1, x[1].get("timestamp", "")))


def save(result: dict, results_dir: Path = config.RESULTS_DIR) -> Path:
    if result.get("label"):
        folder = Path(results_dir) / RERUNS_DIR
        existing = rerun_attempts(folder, result["provider"], result.get("tier"),
                                  result.get("suite", "questions"), result["label"])
        result["attempt"] = max([r.get("attempt") or 1 for _, r in existing], default=0) + 1
    p = result_path(result, results_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    if result.get("label") and p.exists():
        raise FileExistsError(f"refusing to overwrite re-run evidence: {p}")
    p.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return p


def _validate_rerun(base: dict, rr: dict) -> set[str]:
    if (rr["provider"], rr.get("tier"), rr.get("suite", "questions"), rr.get("model")) != \
            (base["provider"], base.get("tier"), base.get("suite", "questions"), base.get("model")):
        raise ValueError("re-run does not match the base run (provider, tier, suite, model)")
    if rr["repeats"] != base["repeats"]:
        raise ValueError("re-run repeats must equal base repeats so pass^k stays comparable")
    base_ids = {r["id"] for r in base["records"]}
    ids = {r["id"] for r in rr["records"]}
    extra = ids - base_ids
    if extra:
        raise ValueError(f"re-run contains case ids that are not in the base run: {sorted(extra)}")
    for cid in ids:
        reps = sorted(r.get("repeat") for r in rr["records"] if r["id"] == cid)
        if reps != list(range(base["repeats"])):
            raise ValueError(f"re-run case {cid} must have exactly {base['repeats']} records "
                             f"(repeats 0..{base['repeats'] - 1}), found repeats {reps}")
    return ids


def merge_results(base: dict, reruns: list[dict], base_name: str = "", rerun_names: list[str] | None = None,
                  attempts_on_disk: dict[str, int] | None = None) -> dict:
    """Derived result: the base run with each re-run case id's records replaced, summary recomputed
    from records (never hand-edited), recorded summary and full provenance kept.

    Anti-cherry-picking rules:
      * every re-run must match the base (provider, tier, suite, model, repeats), may only contain
        case ids that are in the base, and must hold exactly `repeats` records per case;
      * if more attempts of a re-run series exist on disk than were supplied, merge refuses: all
        attempts must be reported;
      * when several attempts are supplied, the EARLIEST attempt containing a case is used (it was
        chosen before any later outcome was seen) and every attempt's score is recorded.
    """
    rerun_names = rerun_names or [f"rerun-{i}" for i in range(len(reruns))]
    groups: dict[str, list[tuple[str, dict, set]]] = {}
    for name, rr in zip(rerun_names, reruns):
        ids = _validate_rerun(base, rr)
        key = rerun_group(rr) if rr.get("label") else f"unlabelled:{name}"
        groups.setdefault(key, []).append((name, rr, ids))

    records = list(base["records"])
    provenance = []
    for key, attempts in groups.items():
        on_disk = (attempts_on_disk or {}).get(key, len(attempts))
        if on_disk > len(attempts):
            raise ValueError(f"{on_disk} attempts of re-run series {key!r} exist but only {len(attempts)} "
                             f"were supplied; report every attempt")
        attempts.sort(key=lambda a: (a[1].get("attempt") or 1, a[1].get("timestamp", "")))
        used: dict[str, str] = {}
        for name, rr, ids in attempts:
            for cid in sorted(ids):
                if cid in used:
                    continue
                used[cid] = name
                records = [r for r in records if r["id"] != cid] + [r for r in rr["records"] if r["id"] == cid]
        provenance.append({
            "series": key,
            "attempts_on_disk": on_disk,
            "attempts_reported": len(attempts),
            "attempts": [{"file": n, "attempt": rr.get("attempt") or 1, "timestamp": rr["timestamp"],
                          "case_ids": sorted(ids), "correct": sum(r["correct"] for r in rr["records"]),
                          "records": len(rr["records"]), "retry_rule": rr.get("retry_rule", "none")}
                         for n, rr, ids in attempts],
            "used": used,
        })

    base_rule = base.get("retry_rule", "none")
    rerun_rules = sorted({rr.get("retry_rule", "none") for rr in reruns})
    if rerun_rules in ([], [base_rule]):
        rule = base_rule
    else:
        rule = f"mixed: base records: {base_rule}; re-run records: {' / '.join(rerun_rules)}"

    out = {k: v for k, v in base.items() if k not in ("summary", "records")}
    out["retry_rule"] = rule
    out["derived_from"] = {
        "base": base_name, "base_timestamp": base["timestamp"],
        "base_retry_rule": base_rule,
        "reruns": [{"file": n, "timestamp": rr["timestamp"], "attempt": rr.get("attempt") or 1,
                    "replaced_case_ids": sorted(ids), "retry_rule": rr.get("retry_rule", "none")}
                   for n, rr, ids in zip(rerun_names, reruns,
                                         [{r["id"] for r in rr["records"]} for rr in reruns])],
        "series": provenance,
        "base_summary": base["summary"],
    }
    out["summary"] = summarize(records, base["repeats"])
    out["records"] = records
    return out


def check_rerun_completeness(base: dict, reruns: list[dict], search: list[tuple[Path, bool]]) -> dict[str, int]:
    """Anti-cherry-picking checks over every stored re-run, not only the supplied files.

    search: (folder, recursive) pairs; the CLI passes <results-dir>/reruns recursively plus the
    folders of the supplied files. Considers every labelled re-run file for the same provider, tier,
    suite and model as the base. Raises ValueError when:
      * a series has a gap in its attempt numbers (an attempt was deleted)            [A7]
      * a stored attempt covering any merged case id was not supplied, whatever its
        label or folder (copying or relabelling a lucky attempt does not hide others) [A6, A8]
    Returns {series: highest attempt number} for merge_results.
    """
    ident = (base["provider"], base.get("tier"), base.get("suite", "questions"), base.get("model"))
    stored: dict[tuple[str, int], set[str]] = {}
    for folder, recursive in search:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for p in sorted(folder.rglob("*.json") if recursive else folder.glob("*.json")):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(r, dict) or not r.get("label") or not isinstance(r.get("records"), list):
                continue
            if (r.get("provider"), r.get("tier"), r.get("suite", "questions"), r.get("model")) != ident:
                continue
            key = (rerun_group(r), int(r.get("attempt") or 1))
            stored.setdefault(key, set()).update(x["id"] for x in r["records"])
    supplied = {}
    for rr in reruns:
        if rr.get("label"):
            supplied[(rerun_group(rr), int(rr.get("attempt") or 1))] = {x["id"] for x in rr["records"]}
    every = {**stored, **supplied}

    series: dict[str, set[int]] = {}
    for g, n in every:
        series.setdefault(g, set()).add(n)
    for g, nums in series.items():
        if sorted(nums) != list(range(1, max(nums) + 1)):
            raise ValueError(f"re-run series {g!r} has a gap in attempt numbers {sorted(nums)}; "
                             f"an attempt is missing")

    merged_ids = set().union(*supplied.values()) if supplied else set()
    missing = sorted(f"{g} attempt {n}" for (g, n), ids in stored.items()
                     if (g, n) not in supplied and ids & merged_ids)
    if missing:
        raise ValueError("stored re-runs of the merged cases were not supplied; report every attempt: "
                         + ", ".join(missing))
    return {g: max(nums) for g, nums in series.items()}


def is_all_error(run: dict) -> bool:
    """True only when EVERY record is a zero-token error with no model output (transport failure,
    CLI-reported API error, failed process). Execution errors and unparseable answers are model
    output: such a run is a (failed) result, never 'not run'."""
    recs = run.get("records") or []
    return bool(recs) and all(pipeline.is_no_output_record(r) for r in recs)


def infra_error_count(run: dict) -> int:
    return sum(pipeline.is_no_output_record(r) for r in run.get("records") or [])


def not_run_reason(run: dict) -> str:
    msg = next((r.get("error", "") for r in run["records"] if r.get("error")), "all calls errored")
    return msg.replace("|", "/")[:160]


REGRADED_DIR = "regraded"


def regrade_result(run: dict, cases: list[dict], con, base_name: str = "", cases_name: str = "") -> dict:
    """Offline regrade with the CURRENT grader. No model calls.

    For every record whose recorded action was 'sql', the stored model SQL is re-executed through
    the current guard and executor and graded against the gold in `cases`. Refusals,
    clarifications, guard blocks and errors keep their recorded action and correctness (the grader
    plays no part in them). The recorded summary and every changed record are kept as provenance.
    """
    by_id = {c["id"]: c for c in cases}
    gold = {}
    for c in cases:
        if "answer" in c["expect"] and c.get("gold_sql"):
            gold[c["id"]] = executor.run(c["gold_sql"], con)
    new_records, changes = [], []
    for rec in run["records"]:
        r = dict(rec)
        if rec["action"] == "sql" and rec["id"] in by_id:
            res = pipeline.AskResult(question=rec["question"], action="sql", sql=rec.get("sql", ""))
            g = guard.check(res.sql)
            if not g.ok:
                ok, why = False, "regrade: stored SQL now blocked by guard: " + "; ".join(g.reasons)
            else:
                try:
                    res.columns, res.rows = executor.run(g.sql, con)
                    gcols, grows = gold.get(rec["id"], ([], []))
                    ok, why = grade_case(by_id[rec["id"]], res, grows, gcols)
                except Exception as exc:
                    ok, why = False, f"regrade: execution error: {exc}"
            if ok != rec["correct"]:
                changes.append({"id": rec["id"], "repeat": rec.get("repeat"), "recorded_correct": rec["correct"],
                                "regraded_correct": ok, "reason": why})
            r["correct"], r["reason"] = ok, why
        new_records.append(r)
    out = {k: v for k, v in run.items() if k not in ("summary", "records")}
    out["retry_rule"] = run.get("retry_rule", "none")
    out["grader_version"] = grader.GRADER_VERSION
    out["regraded_from"] = {
        "file": base_name,
        "recorded_timestamp": run["timestamp"],
        "recorded_grader_version": run.get("grader_version", "v1 (pre-round-1, not recorded in file)"),
        "cases_file": cases_name,
        "method": "offline: stored SQL re-executed through the current guard and executor, graded by the "
                  "current grader; no model calls; non-SQL records unchanged",
        "changed_records": changes,
        "recorded_summary": run["summary"],
    }
    out["summary"] = summarize(new_records, run["repeats"])
    out["records"] = new_records
    return out


def _fmt(v, suffix=""):
    return "n/a" if v is None else f"{v}{suffix}"


def _load_runs(folder: Path) -> list[tuple[str, dict]]:
    runs = []
    for p in sorted(folder.glob("*.json")):
        try:
            runs.append((p.name, json.loads(p.read_text(encoding="utf-8"))))
        except json.JSONDecodeError:
            continue
    return runs


def _not_run_row(prov, model, suite, reason):
    cells = " | ".join([f"not run: {reason}"] + ["not run"] * 9)
    return f"| {prov} | {model} | {suite} | - | {cells} |"


def _summary_row(name_cells: str, r: dict) -> str:
    s = r["summary"]
    passk = next(v for k, v in s.items() if k.startswith("pass^"))
    cost = s["api_equivalent_cost_usd"]
    if cost is not None:
        cost_txt = f"${cost}"
    else:
        cost_txt = "n/a (offline)" if r["provider"] == "baseline" else "not reported by CLI"
    overall = _fmt(s["overall_accuracy_pct"], "%")
    k = infra_error_count(r)
    if k:
        # Partial infrastructure failure: still a result, but flagged so it is not read as model quality.
        overall += f" (incl. {k}/{len(r['records'])} infra errors)"
    return (f"| {name_cells} | {r['repeats']} | {overall} | "
            f"{_fmt(s['execution_accuracy_pct'], '%')} | {_fmt(s['refusal_correctness_pct'], '%')} | "
            f"{_fmt(s['destructive_blocked_pct'], '%')} | {_fmt(passk, '%')} | {s['latency_p50_s']} | "
            f"{s['latency_p95_s']} | {s['tokens_per_question']} | {cost_txt} |")


HEADER = ("| Provider | Model | Suite | Repeats | Overall acc | Exec acc (answerable) | Refusal correctness | "
          "Destructive blocked | pass^k | p50 latency (s) | p95 latency (s) | Tokens / question | "
          f"{COST_LABEL} |")
RULE = "|---|---|---|---|---|---|---|---|---|---|---|---|---|"


def render_report(results_dir: Path = config.RESULTS_DIR) -> str:
    results_dir = Path(results_dir)
    loaded = _load_runs(results_dir)
    lines = ["# Eval report", "",
             "Contoso Insurance (fictional sample data). Execution-graded: gold and predicted SQL are both run",
             "and their result sets compared (order-insensitive; integers exact, other numbers relative",
             "tolerance 1e-3).", "",
             f"Latency quantiles: {QUANTILE_METHOD}. Latency varies by run and machine; no mean or max is",
             "reported. Tokens include checker calls; cost covers generation calls only.", "",
             "## Tier table (recorded full runs)", "", HEADER, RULE]
    seen = set()
    full_runs = []
    for _, r in loaded:
        suite = r.get("suite", "questions")
        if is_all_error(r):
            # An all-error run is not a result: never render it as 0%.
            lines.append(_not_run_row(r["provider"], r["model"], suite, not_run_reason(r)))
        else:
            lines.append(_summary_row(f"{r['provider']} | {r['model']} | {suite}", r))
            full_runs.append(r)
        seen.add((r["provider"], r.get("tier"), suite))
    for prov, tier, model in TIER_ROWS:
        for suite in SUITES:
            if (prov, tier, suite) not in seen:
                lines.append(_not_run_row(prov, model, suite, config.NOT_RUN_REASONS.get(tier, "no run recorded")))

    reruns = _load_runs(results_dir / RERUNS_DIR)
    derived = _load_runs(results_dir / DERIVED_DIR)
    if reruns:
        lines += ["", "## Targeted re-runs (results/reruns/)", "",
                  "Only the listed case ids were re-run. These are not full runs.", "",
                  "| File | Model | Cases | Repeats | Correct / records | Infra retries used | Retry rule |",
                  "|---|---|---|---|---|---|---|"]
        for name, r in reruns:
            n = len(r["records"])
            ok = sum(x["correct"] for x in r["records"])
            lines.append(f"| {name} | {r['model']} | {', '.join(r.get('case_ids', []))} | {r['repeats']} | "
                         f"{ok}/{n} | {r['summary'].get('infra_retries', 0)} | {r.get('retry_rule', 'none')} |")
    if derived:
        lines += ["", "## Derived results (results/derived/): recorded run with re-run cases substituted", "",
                  "Recomputed from records by `python -m nl2sql merge`; the recorded run is shown first.", "",
                  HEADER, RULE]
        for name, r in derived:
            d = r["derived_from"]
            base_view = dict(r, summary=d["base_summary"])
            lines.append(_summary_row(f"{r['provider']} | {r['model']} | {r.get('suite', 'questions')} "
                                      f"(recorded: {d['base']})", base_view))
            ids = "; ".join(f"{', '.join(x['replaced_case_ids'])} from {x['file']}" for x in d["reruns"])
            lines.append(_summary_row(f"{r['provider']} | {r['model']} | {r.get('suite', 'questions')} "
                                      f"({name}: {ids})", r))

    regraded = _load_runs(results_dir / REGRADED_DIR)
    regraded_derived = _load_runs(results_dir / REGRADED_DIR / DERIVED_DIR)
    if regraded:
        lines += ["", f"## Offline regrade with grader {grader.GRADER_VERSION} (results/regraded/)", "",
                  "Stored SQL re-executed and graded by the current grader; no model calls. Recorded row first.",
                  "", HEADER, RULE]
        for name, r in regraded:
            g = r["regraded_from"]
            lines.append(_summary_row(f"{r['provider']} | {r['model']} | {r.get('suite', 'questions')} "
                                      f"(recorded: {g['file']})", dict(r, summary=g["recorded_summary"])))
            lines.append(_summary_row(f"{r['provider']} | {r['model']} | {r.get('suite', 'questions')} "
                                      f"(regraded: {name}; {len(g['changed_records'])} record(s) changed)", r))
    if regraded_derived:
        lines += ["", "## Derived results on regraded inputs (results/regraded/derived/)", "", HEADER, RULE]
        for name, r in regraded_derived:
            lines.append(_summary_row(f"{r['provider']} | {r['model']} | {r.get('suite', 'questions')} ({name})", r))

    for r in full_runs:
        s = r["summary"]
        lines += ["", f"## {r['provider']} / {r['model']} / suite {r.get('suite', 'questions')} ({r['timestamp']})", "",
                  "| Category | Correct | N | Accuracy |", "|---|---|---|---|"]
        for cat, v in s["by_category"].items():
            lines.append(f"| {cat} | {v['correct']} | {v['n']} | {_fmt(v['accuracy_pct'], '%')} |")
        ck = s["checker"]
        lines += ["", "**Adversarial checker** (independent control-total query, never sees gold SQL):", "",
                  f"- answered: {ck['answered']}; wrong answers: {ck['wrong_answers']}; "
                  f"caught (flagged): {ck['wrong_caught']}; abstained on wrong: {ck['wrong_abstained']}; "
                  f"caught rate: {_fmt(ck['caught_rate_pct'], '%')}",
                  f"- correct answers: {ck['correct_answers']}; confirmed (agree): {ck['agree_on_correct']}; "
                  f"false alarms: {ck['false_alarms']}",
                  "", "**Failures**", ""]
        fails = [x for x in r["records"] if not x["correct"]]
        if not fails:
            lines.append("- none")
        for x in fails:
            lines.append(f"- `{x['id']}` (repeat {x['repeat']}, {x['category']}): {x['reason']}; "
                         f"checker: {x['check']['status']}")
    return "\n".join(lines) + "\n"


def write_report(results_dir: Path = config.RESULTS_DIR) -> Path:
    p = Path(results_dir) / "report.md"
    p.write_text(render_report(results_dir), encoding="utf-8")
    return p
