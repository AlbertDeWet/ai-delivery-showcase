"""CLI: python -m smguard <generate|analyze|trim|regress|consumers|demo>"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="smguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    for name in ("analyze", "trim", "regress", "consumers"):
        p = sub.add_parser(name)
        p.add_argument("env", choices=["DEV", "UAT", "PROD"])
        if name == "trim":
            g = p.add_mutually_exclusive_group(required=True)
            g.add_argument("--plan", action="store_true", help="print the plan only")
            g.add_argument("--apply", action="store_true", help="backup + apply to a copy in work/")
    sub.add_parser("drift")
    sub.add_parser("demo")
    a = ap.parse_args(argv)

    from . import analyze as an, generate as gen, regress, trim
    models, data, reports = ROOT / "models", ROOT / "data", ROOT / "reports"
    if a.cmd == "generate":
        gen.generate(ROOT)
        print("generated models/, data/, reports/")
    elif a.cmd == "analyze":
        r = an.analyze(models / a.env, data / a.env / "v1", reports)
        r.pop("column_detail")
        print(json.dumps(r, indent=2))
    elif a.cmd == "consumers":
        r = an.analyze(models / a.env, data / a.env / "v1", reports)["consumers"]
        print(json.dumps(r, indent=2))
        return 0 if r["passed"] == r["reports"] else 1
    elif a.cmd == "drift":
        print(json.dumps(an.drift({e: models / e for e in gen.ENVS}), indent=2))
    elif a.cmd == "trim":
        try:
            p = trim.plan(models / a.env, reports)
        except trim.UnsupportedFeature as e:
            print(json.dumps({"safe": False, "refused": str(e)}, indent=2))
            return 2
        if a.plan:
            print(json.dumps(p, indent=2))
        else:
            out = trim.apply(models / a.env, data / a.env / "v1", reports, ROOT / "work" / a.env, p)
            print(json.dumps({"backup": out["backup"], "trimmed": out["trimmed"],
                              "checks": out["checks"], "safe": out["safe"]}, indent=2))
            return 0 if out["safe"] else 1
    elif a.cmd == "regress":
        trimmed = ROOT / "work" / a.env / "trimmed"
        if not trimmed.exists():
            print("run `trim ENV --apply` first")
            return 2
        d = data / a.env / "v1"
        rows = regress.classify(regress.run_suite(models / a.env, d), regress.run_suite(trimmed, d),
                                regress.run_suite(trimmed, d))
        print(json.dumps(rows, indent=2))
        return 1 if any(r["class"] == "UNEXPECTED" for r in rows) else 0
    elif a.cmd == "demo":
        from .demo import run
        s = run(ROOT)
        print((ROOT / "results" / "summary.md").read_text(encoding="utf-8"))
        # 0 = all safe, 3 = only waiting for human sign-off (trim blocked), 1 = blocked by a problem
        if all(e["safe"] for e in s["envs"].values()):
            return 0
        if all(e["status"].startswith("pending human sign-off") for e in s["envs"].values() if not e["safe"]):
            return 3
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
