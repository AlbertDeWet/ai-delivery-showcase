"""Seed three local workspaces with Contoso Insurance (fictional sample data) and deliberate drift.

DEV is the reviewed source of truth. UAT has a little drift, PROD has more. A protected team
namespace (schema team_b, jobs team_b_*) differs between environments and must never be touched.
"""
from __future__ import annotations

import json
import random
import zlib
import shutil
from pathlib import Path

import duckdb
import yaml

from .workspace import DEFAULT_ROOT, ENVS, Workspace

PRODUCTS = [("motor_std", "motor"), ("motor_fleet", "motor"), ("home_plus", "property"),
            ("commercial_prop", "property"), ("public_liab", "liability"), ("marine_cargo", "marine"),
            ("travel_basic", "travel"), ("travel_gold", "travel")]
REGIONS = ["north", "south", "east", "west", "central"]


def _views_dev() -> dict[str, str]:
    v: dict[str, str] = {}
    for s in ("active", "lapsed", "cancelled"):
        v[f"v_policy_{s}"] = f"SELECT * FROM core.policy WHERE status = '{s}'"
    for line in ("motor", "property", "liability", "marine", "travel"):
        v[f"v_policy_line_{line}"] = (
            "SELECT p.* FROM core.policy p JOIN core.product r ON p.product = r.product "
            f"WHERE r.line = '{line}'")
    for s in ("open", "closed", "reopened", "rejected"):
        v[f"v_claims_{s}"] = f"SELECT * FROM core.claim WHERE status = '{s}'"
    for r in REGIONS:
        v[f"v_broker_{r}"] = f"SELECT * FROM core.broker WHERE region = '{r}'"
    v.update({
        "v_premium_by_broker": "SELECT broker_id, SUM(premium) AS premium FROM core.policy GROUP BY broker_id",
        "v_premium_by_product": "SELECT product, SUM(premium) AS premium FROM core.policy GROUP BY product",
        "v_premium_by_region": ("SELECT b.region, SUM(p.premium) AS premium FROM core.policy p "
                                "JOIN core.broker b ON p.broker_id = b.broker_id GROUP BY b.region"),
        "v_claims_by_product": ("SELECT p.product, SUM(c.amount) AS incurred FROM core.claim c "
                                "JOIN core.policy p ON c.policy_id = p.policy_id GROUP BY p.product"),
        "v_claim_ratio_by_product": ("SELECT pr.product, COALESCE(cl.incurred, 0) / pr.premium AS loss_ratio "
                                     "FROM core.v_premium_by_product pr LEFT JOIN core.v_claims_by_product cl "
                                     "ON pr.product = cl.product"),
        "v_large_claims": "SELECT * FROM core.claim WHERE amount >= 50000",
        "v_claim_aging": ("SELECT claim_id, status, DATE '2026-06-30' - reported_date AS age_days "
                          "FROM core.claim WHERE status IN ('open', 'reopened')"),
        "v_policy_count_by_month": ("SELECT date_trunc('month', start_date) AS month, COUNT(*) AS n "
                                    "FROM core.policy GROUP BY 1"),
        "v_new_business": "SELECT * FROM core.policy WHERE start_date >= DATE '2026-01-01'",
        "v_renewals_due": ("SELECT * FROM core.policy WHERE status = 'active' "
                           "AND start_date < DATE '2025-07-01'"),
        "v_lapse_rate": ("SELECT AVG(CASE WHEN status = 'lapsed' THEN 1.0 ELSE 0.0 END) AS lapse_rate "
                         "FROM core.policy"),
        "v_claims_per_policy": "SELECT policy_id, COUNT(*) AS n_claims FROM core.claim GROUP BY policy_id",
        "v_avg_claim_by_status": "SELECT status, AVG(amount) AS avg_amount FROM core.claim GROUP BY status",
        "v_broker_league": ("SELECT b.name, SUM(p.premium) AS premium FROM core.policy p JOIN core.broker b "
                            "ON p.broker_id = b.broker_id GROUP BY b.name ORDER BY premium DESC"),
        "v_product_catalogue": "SELECT product, line FROM core.product",
        "v_policy_no_claims": ("SELECT p.* FROM core.policy p WHERE NOT EXISTS "
                               "(SELECT 1 FROM core.claim c WHERE c.policy_id = p.policy_id)"),
        "v_fx_rates_active": "SELECT key AS currency, value AS source FROM core.cfg_fx_source WHERE active",
        "v_report_mapping": "SELECT key, value FROM core.cfg_report_map WHERE active",
        "v_kpi_thresholds": "SELECT key AS kpi, CAST(value AS DOUBLE) AS threshold FROM core.cfg_kpi_threshold",
        "v_claims_last_quarter": "SELECT * FROM core.claim WHERE reported_date >= DATE '2026-04-01'",
        "v_broker_new": "SELECT * FROM core.broker WHERE broker_id > 35",  # added in DEV, not yet promoted
    })
    return v


CFG_NAMES = ["cfg_environment", "cfg_fx_source", "cfg_report_map", "cfg_kpi_threshold", "cfg_product_mapping",
             "cfg_region_mapping", "cfg_status_mapping", "cfg_currency", "cfg_calendar", "cfg_segment",
             "cfg_channel", "cfg_lob_mapping", "cfg_reinsurance_treaty", "cfg_tax_rate", "cfg_commission",
             "cfg_large_loss_limit", "cfg_reserve_factor", "cfg_ibnr_method", "cfg_report_period", "cfg_gl_account",
             "cfg_cost_centre", "cfg_branch", "cfg_risk_class", "cfg_cover_type", "cfg_claim_cause",
             "cfg_payment_method", "cfg_distribution", "cfg_rounding", "cfg_feature_flags", "cfg_email_routes"]


def _cfg_dev(rng: random.Random, env: str) -> dict[str, list[tuple]]:
    out = {}
    for name in CFG_NAMES:
        if name == "cfg_environment":
            out[name] = [("catalog", f"contoso_{env}", True),
                         ("landing_path", f"lake://contoso_{env}/landing", True),
                         ("host", f"https://{env}.contoso-insurance.example", True)]
            continue
        n = rng.randint(4, 12)
        out[name] = [(f"{name[4:]}_{i:02d}", f"val_{rng.randint(100, 999)}", rng.random() > 0.1) for i in range(n)]
    out["cfg_kpi_threshold"] = [(f"kpi_{i:02d}", str(round(rng.uniform(0.1, 0.9), 2)), True) for i in range(8)]
    out["cfg_fx_source"] = [(c, "central_bank_feed", True) for c in ("USD", "EUR", "GBP", "ZAR", "AUD", "JPY")]
    return out


def _jobs(env: str) -> dict[str, dict]:
    def job(name, cron, tasks, uses, workers=2, paused=None):
        return {
            "job_id": zlib.crc32(f"{name}:{env}".encode()) % 10**9,
            "created_time": 1760000000000,
            "name": name,
            "tags": {"owner": "team_b" if name.startswith("team_b_") else "reporting",
                     "uses_objects": ",".join(uses)},
            "schedule": {"quartz_cron_expression": cron, "timezone_id": "UTC",
                         "pause_status": paused or ("UNPAUSED" if env == "prod" else "PAUSED")},
            "job_clusters": [{"job_cluster_key": "main", "new_cluster": {"spark_version": "15.4.x-scala2.12",
                                                                          "node_type_id": "Standard_D4ds_v5",
                                                                          "num_workers": workers}}],
            "tasks": [{"task_key": t, "job_cluster_key": "main",
                       "notebook_task": {"notebook_path": f"/Workspace/bundles/contoso_reporting/{env}/src/{t}"}}
                      for t in tasks],
            "email_notifications": {"on_failure": [f"reporting-oncall-{env}@contoso-insurance.example"]},
        }
    w = 4 if env == "prod" else 1
    return {
        # Runs every 30 minutes and recreates/reads reporting views: the core-job risk.
        "reporting_views_refresh": job("reporting_views_refresh", "0 0/30 * * * ?", ["refresh_views", "publish"],
                                       ["core.v_claim_aging", "core.v_policy_active", "core.v_premium_by_broker",
                                        "core.v_claim_ratio_by_product"], workers=w, paused="UNPAUSED"),
        "claims_daily": job("claims_daily", "0 0 2 * * ?", ["load_claims", "validate_claims"],
                            ["core.v_claims_open", "core.v_large_claims"], workers=w),
        "policy_daily": job("policy_daily", "0 30 2 * * ?", ["load_policy", "validate_policy"],
                            ["core.v_policy_active", "core.v_new_business"], workers=w),
        "fx_load": job("fx_load", "0 0 5 * * ?", ["load_fx"], ["core.cfg_fx_source", "core.v_fx_rates_active"]),
        "month_end_close": job("month_end_close", "0 0 6 1 * ?", ["close_period", "publish_pack"],
                               ["core.v_claim_ratio_by_product", "core.cfg_report_period"], workers=w),
        "team_b_ingest": job("team_b_ingest", "0 15 * * * ?", ["ingest", "reconcile"], ["team_b.v_team_b_summary"]),
    }


def _bundle() -> dict:
    return {
        "bundle": {"name": "contoso_reporting"},
        "variables": {"catalog": {"default": "contoso_dev"}, "warehouse_size": {"default": "small"}},
        "include": ["resources/*.yml"],
        "resources": {"jobs": {n: {"name": n, "file": f"jobs/{n}.json"} for n in
                               ["reporting_views_refresh", "claims_daily", "policy_daily", "fx_load", "month_end_close"]}},
        "targets": {
            "dev": {"mode": "development", "default": True,
                    "workspace": {"host": "https://dev.contoso-insurance.example"},
                    "variables": {"catalog": "contoso_dev"}},
            "uat": {"mode": "production", "workspace": {"host": "https://uat.contoso-insurance.example"},
                    "variables": {"catalog": "contoso_uat"},
                    "run_as": {"service_principal_name": "sp-contoso-reporting-uat"}},
            "prod": {"mode": "production", "workspace": {"host": "https://prod.contoso-insurance.example"},
                     "variables": {"catalog": "contoso_prod"},
                     "run_as": {"service_principal_name": "sp-contoso-reporting-prod"}},
        },
    }


def _data(con, rng: random.Random):
    con.execute("CREATE TABLE core.product(product VARCHAR, line VARCHAR)")
    con.executemany("INSERT INTO core.product VALUES (?,?)", PRODUCTS)
    con.execute("CREATE TABLE core.broker(broker_id INTEGER, name VARCHAR, region VARCHAR)")
    con.executemany("INSERT INTO core.broker VALUES (?,?,?)",
                    [(i, f"Broker {i:02d} (fictional)", REGIONS[i % 5]) for i in range(1, 41)])
    con.execute("CREATE TABLE core.policy(policy_id INTEGER, product VARCHAR, broker_id INTEGER, "
                "status VARCHAR, premium DOUBLE, start_date DATE)")
    pol = []
    for i in range(1, 301):
        pol.append((i, rng.choice(PRODUCTS)[0], rng.randint(1, 40),
                    rng.choices(["active", "lapsed", "cancelled"], [7, 2, 1])[0],
                    round(rng.uniform(500, 20000), 2), f"202{rng.randint(4, 6)}-{rng.randint(1, 12):02d}-15"))
    con.executemany("INSERT INTO core.policy VALUES (?,?,?,?,?,?)", pol)
    con.execute("CREATE TABLE core.claim(claim_id INTEGER, policy_id INTEGER, status VARCHAR, "
                "amount DOUBLE, reported_date DATE)")
    cl = [(i, rng.randint(1, 300), rng.choice(["open", "closed", "reopened", "rejected"]),
           round(rng.uniform(200, 90000), 2), f"2026-{rng.randint(1, 6):02d}-{rng.randint(1, 28):02d}")
          for i in range(1, 181)]
    con.executemany("INSERT INTO core.claim VALUES (?,?,?,?,?)", cl)


def _team_b(con, env: str):
    """Protected team's namespace. Deliberately different per env - that is THEIR business."""
    con.execute("CREATE SCHEMA team_b")
    con.execute("CREATE TABLE team_b.cfg_team_b_settings(key VARCHAR, value VARCHAR, active BOOLEAN)")
    rows = [("batch_size", {"dev": "100", "uat": "500", "prod": "5000"}[env], True),
            ("feed", "partner_feed_v2" if env != "prod" else "partner_feed_v1", True)]
    con.executemany("INSERT INTO team_b.cfg_team_b_settings VALUES (?,?,?)", rows)
    con.execute("CREATE TABLE team_b.ingest_log(run_id INTEGER, rows_loaded INTEGER)")
    con.executemany("INSERT INTO team_b.ingest_log VALUES (?,?)", [(i, i * 10) for i in range(1, 6 if env == "prod" else 3)])
    flt = "rows_loaded > 0" if env != "dev" else "rows_loaded >= 0"
    con.execute(f"CREATE VIEW team_b.v_team_b_summary AS SELECT COUNT(*) AS runs, SUM(rows_loaded) AS rows "
                f"FROM team_b.ingest_log WHERE {flt}")


def _apply_drift(env: str, views: dict, cfg: dict, jobs: dict, bundle: dict):
    if env == "dev":
        return
    # --- views -------------------------------------------------------------------------
    views.pop("v_broker_new")  # new in DEV, not promoted anywhere yet
    views["v_claim_aging"] = views["v_claim_aging"].replace("('open', 'reopened')", "('open')")  # stale
    # cosmetic-only difference (must be classified identical, not drift)
    views["v_lapse_rate"] = views["v_lapse_rate"].upper().replace("'LAPSED'", "'lapsed'").replace("  ", " ")
    if env == "prod":
        views["v_large_claims"] = views["v_large_claims"].replace("50000", "25000")  # hotfixed in prod only
        views["v_premium_by_broker"] = ("SELECT broker_id, SUM(premium) AS premium, COUNT(*) AS n "
                                        "FROM core.policy GROUP BY broker_id")
        views["v_renewals_due"] = views["v_renewals_due"].replace("2025-07-01", "2025-06-01")
        views.pop("v_claims_last_quarter")
        views["v_tmp_debug_claims"] = "SELECT * FROM core.claim LIMIT 10"  # orphan, prod only
    # --- config tables --------------------------------------------------------------------
    cfg["cfg_report_map"] = cfg["cfg_report_map"][:-2]  # two keys missing
    cfg["cfg_fx_source"] = [(k, "manual_override" if k == "ZAR" else v, a) for k, v, a in cfg["cfg_fx_source"]]
    if env == "prod":
        for name in ("cfg_tax_rate", "cfg_commission", "cfg_reserve_factor", "cfg_segment"):
            k, v, a = cfg[name][0]
            cfg[name][0] = (k, v + "_old", a)
        cfg["cfg_kpi_threshold"] = cfg["cfg_kpi_threshold"] + [("kpi_99", "0.5", True)]  # extra key
        del cfg["cfg_feature_flags"]  # whole table missing
        cfg["cfg_channel"] = [(k, v, a, "legacy") for k, v, a in cfg["cfg_channel"]]  # schema drift
    # --- jobs ------------------------------------------------------------------------------
    if env == "prod":
        t = jobs["claims_daily"]["tasks"][1]
        t["notebook_task"]["notebook_path"] = "/Workspace/Users/someone/scratch/validate_claims_v2"
        jobs["month_end_close"]["tasks"] = jobs["month_end_close"]["tasks"][:1]  # publish task missing
    if env == "uat":
        jobs["fx_load"]["schedule"]["quartz_cron_expression"] = "0 0 4 * * ?"
    # --- bundle ----------------------------------------------------------------------------
    if env == "prod":
        del bundle["targets"]["prod"]["run_as"]
        bundle["variables"]["warehouse_size"]["default"] = "medium"


def seed(root: Path | str | None = None, rng_seed: int = 42) -> Path:
    root = Path(root) if root else DEFAULT_ROOT
    if root.exists():
        shutil.rmtree(root)
    for env in ENVS:
        ws = Workspace(root, env)
        ws.jobs_dir.mkdir(parents=True)
        rng = random.Random(rng_seed)  # same base data in every env
        views, cfg, jobs, bundle = _views_dev(), _cfg_dev(rng, env), _jobs(env), _bundle()
        _apply_drift(env, views, cfg, jobs, bundle)
        con = duckdb.connect(str(ws.db_path))
        try:
            con.execute("CREATE SCHEMA core")
            _data(con, random.Random(rng_seed + 1))
            for name, rows in cfg.items():
                extra = ", tier VARCHAR" if rows and len(rows[0]) == 4 else ""
                con.execute(f"CREATE TABLE core.{name}(key VARCHAR, value VARCHAR, active BOOLEAN{extra})")
                ph = ",".join("?" * len(rows[0])) if rows else ""
                if rows:
                    con.executemany(f"INSERT INTO core.{name} VALUES ({ph})", rows)
            pending = dict(views)
            # create views in dependency order (simple retry loop)
            for _ in range(5):
                for name in list(pending):
                    try:
                        con.execute(f"CREATE VIEW core.{name} AS {pending[name]}")
                        pending.pop(name)
                    except duckdb.Error:
                        pass
            if pending:
                raise RuntimeError(f"could not create views: {sorted(pending)}")
            _team_b(con, env)
        finally:
            con.close()
        for name, j in jobs.items():
            (ws.jobs_dir / f"{name}.json").write_text(json.dumps(j, indent=2), encoding="utf-8")
        ws.bundle_path.write_text(yaml.safe_dump(bundle, sort_keys=False), encoding="utf-8")
    return root


if __name__ == "__main__":
    print(f"seeded {seed()}")
