"""Command line: generate-data | ask | eval | report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nl2sql import config


def _provider(args):
    from nl2sql.providers import get_provider
    from nl2sql.providers.claude_cli import cli_available

    if args.provider in ("claude-cli", "claude"):
        if not cli_available(args.claude_cli):
            print(f"Claude Code CLI not found at {args.claude_cli}. Set NL2SQL_CLAUDE_CLI or use "
                  f"--provider baseline.", file=sys.stderr)
            raise SystemExit(2)
        return get_provider("claude-cli", tier=args.tier, cli=args.claude_cli)
    return get_provider("baseline")


def _add_provider_args(p):
    p.add_argument("--provider", choices=["baseline", "claude-cli", "claude"], default="baseline",
                   help="baseline = offline rules (no model); claude-cli = Claude Code CLI on your subscription")
    p.add_argument("--tier", choices=sorted(config.TIERS), default="haiku")
    p.add_argument("--claude-cli", default=config.CLAUDE_CLI, help="path to the claude executable")
    p.add_argument("--db", default=str(config.DB_PATH))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nl2sql", description="Contoso Insurance NL-to-SQL with execution-graded evals")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate-data", help="build the synthetic DuckDB database")
    g.add_argument("--db", default=str(config.DB_PATH))
    g.add_argument("--seed", type=int, default=config.SEED)

    a = sub.add_parser("ask", help="answer one question")
    a.add_argument("question")
    a.add_argument("--json", action="store_true", help="print the full result as JSON")
    _add_provider_args(a)

    e = sub.add_parser("eval", help="run the eval suite")
    _add_provider_args(e)
    e.add_argument("--repeats", type=int, default=1)
    e.add_argument("--cases", default=str(config.EVAL_FILE))
    e.add_argument("--only", nargs="*", help="run only these case ids")
    e.add_argument("--no-checker", action="store_true")
    e.add_argument("--results-dir", default=str(config.RESULTS_DIR))
    e.add_argument("--label", help="targeted re-run label; saved under results/reruns/, never over a full run")
    e.add_argument("--no-retry-infra", action="store_true",
                   help="disable the infra-only retry (1 retry on CLI timeout / 0-token errors)")

    mg = sub.add_parser("merge", help="derive a result: recorded run with re-run cases substituted")
    mg.add_argument("--base", required=True)
    mg.add_argument("--rerun", required=True, nargs="+")
    mg.add_argument("--out", required=True)
    mg.add_argument("--results-dir", default=str(config.RESULTS_DIR))

    rg = sub.add_parser("regrade", help="offline regrade of a recorded run with the current grader (no model calls)")
    rg.add_argument("--input", required=True)
    rg.add_argument("--cases", required=True, help="the eval set the run was recorded against")
    rg.add_argument("--out", required=True)
    rg.add_argument("--db", default=str(config.DB_PATH))
    rg.add_argument("--results-dir", default=str(config.RESULTS_DIR))

    r = sub.add_parser("report", help="rebuild results/report.md from results/*.json")
    r.add_argument("--results-dir", default=str(config.RESULTS_DIR))

    args = ap.parse_args(argv)

    from nl2sql import synth

    if args.cmd == "generate-data":
        path = synth.build_database(Path(args.db), args.seed)
        print(f"wrote {path}")
        return 0

    if args.cmd == "merge":
        from nl2sql import evals

        base = json.loads(Path(args.base).read_text(encoding="utf-8"))
        reruns = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.rerun]
        # Every stored attempt is counted across ALL of <results-dir>/reruns/ (recursive) plus the
        # folders of the supplied files, whatever path is passed (A6). Gaps in attempt numbering are
        # refused (A7), and so is any stored re-run of a merged case for this base that was not
        # supplied, whatever its label (A8).
        search = [(Path(args.results_dir) / evals.RERUNS_DIR, True)] + [(Path(p).parent, False) for p in args.rerun]
        try:
            on_disk = evals.check_rerun_completeness(base, reruns, search)
            out = evals.merge_results(base, reruns, base_name=Path(args.base).name,
                                      rerun_names=[Path(p).name for p in args.rerun], attempts_on_disk=on_disk)
        except ValueError as exc:
            print(f"merge refused: {exc}", file=sys.stderr)
            return 2
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        evals.write_report(Path(args.results_dir))
        print(f"wrote {args.out}")
        print(json.dumps({"recorded": {k: out["derived_from"]["base_summary"][k] for k in
                                       ("overall_accuracy_pct", "execution_accuracy_pct")},
                          "derived": {k: out["summary"][k] for k in
                                      ("overall_accuracy_pct", "execution_accuracy_pct")}}, indent=2))
        return 0

    if args.cmd == "regrade":
        from nl2sql import evals, executor

        run = json.loads(Path(args.input).read_text(encoding="utf-8"))
        cases = evals.load_cases(Path(args.cases))
        con = executor.connect(synth.ensure_database(Path(args.db)))
        try:
            out = evals.regrade_result(run, cases, con, base_name=Path(args.input).name,
                                       cases_name=Path(args.cases).as_posix())
        finally:
            con.close()
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        evals.write_report(Path(args.results_dir))
        ch = out["regraded_from"]["changed_records"]
        rs, ns = out["regraded_from"]["recorded_summary"], out["summary"]
        print(f"wrote {args.out}: {len(ch)} record(s) changed; overall {rs['overall_accuracy_pct']} -> "
              f"{ns['overall_accuracy_pct']}, exec {rs['execution_accuracy_pct']} -> {ns['execution_accuracy_pct']}")
        for c in ch:
            print(f"  {c['id']} rep {c['repeat']}: {c['recorded_correct']} -> {c['regraded_correct']} ({c['reason'][:120]})")
        return 0

    if args.cmd == "report":
        from nl2sql import evals

        print(f"wrote {evals.write_report(Path(args.results_dir))}")
        return 0

    db = synth.ensure_database(Path(args.db))
    provider = _provider(args)

    if args.cmd == "ask":
        from nl2sql import executor, pipeline

        con = executor.connect(db)
        try:
            res = pipeline.ask(args.question, provider, con)
        finally:
            con.close()
        if args.json:
            print(json.dumps(res.to_dict(), indent=2, default=str))
        else:
            print(f"[{provider.name}/{provider.model}] action={res.action} latency={res.latency_s:.2f}s "
                  f"tokens={res.input_tokens}+{res.output_tokens}")
            if res.guarded_sql:
                print("SQL:\n" + res.guarded_sql)
            print(res.answer or res.error)
        return 0 if res.action != "error" else 1

    if args.cmd == "eval":
        from nl2sql import evals

        cases = evals.load_cases(Path(args.cases))
        if args.only:
            cases = [c for c in cases if c["id"] in set(args.only)]
        result = evals.run_eval(provider, repeats=args.repeats, cases=cases, db_path=db,
                                run_checker=not args.no_checker, suite=Path(args.cases).stem,
                                retry_infra=not args.no_retry_infra, label=args.label)
        out = evals.save(result, Path(args.results_dir))
        rep = evals.write_report(Path(args.results_dir))
        s = result["summary"]
        print(f"wrote {out} and {rep}")
        print(json.dumps({k: v for k, v in s.items() if k != "by_category"}, indent=2))
        print("by category:")
        for cat, v in s["by_category"].items():
            print(f"  {cat:28s} {v['correct']:3d}/{v['n']:<3d} {v['accuracy_pct']}%")
        return 0
    return 1
