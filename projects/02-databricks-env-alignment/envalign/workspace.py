"""Workspace layout and low-level readers.

workspaces/<env>/catalog.duckdb   schemas: core (views, data, cfg_* tables), team_b (protected), _backup
workspaces/<env>/databricks.yml   deployed copy of the asset bundle
workspaces/<env>/jobs/*.json      job definitions as deployed in that env
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = PROJECT_ROOT / "workspaces"
ENVS = ("dev", "uat", "prod")
PROTECTED_SCHEMAS = frozenset({"team_b"})
PROTECTED_JOB_PREFIXES = ("team_b_",)
MANAGED_SCHEMAS = ("core",)
BACKUP_SCHEMA = "_backup"
DUCKDB_CONFIG = {"threads": 1}


def is_protected(obj: str) -> bool:
    """True if an object name (schema.name, job name or file name) belongs to the protected team."""
    o = obj.lower().strip()
    if "." in o and o.split(".", 1)[0] in PROTECTED_SCHEMAS:
        return True
    if any(o.startswith(p) for p in PROTECTED_JOB_PREFIXES):
        return True
    return any(f"{s}." in o for s in PROTECTED_SCHEMAS)


@dataclass(frozen=True)
class Workspace:
    root: Path
    env: str

    @property
    def path(self) -> Path:
        return Path(self.root) / self.env

    @property
    def db_path(self) -> Path:
        return self.path / "catalog.duckdb"

    @property
    def jobs_dir(self) -> Path:
        return self.path / "jobs"

    @property
    def bundle_path(self) -> Path:
        return self.path / "databricks.yml"

    def connect(self, read_only: bool = True) -> duckdb.DuckDBPyConnection:
        # threads=1: these catalogs are tiny, and on Windows the thread-pool start/stop dominated every
        # open/close (seconds each). Measured, see README.
        return duckdb.connect(str(self.db_path), read_only=read_only, config=DUCKDB_CONFIG)

    # ---- readers -------------------------------------------------------------
    def views(self, con=None) -> dict[str, str]:
        own = con is None
        con = con or self.connect()
        try:
            rows = con.execute(
                "SELECT schema_name, view_name, sql FROM duckdb_views() "
                "WHERE NOT internal AND schema_name NOT IN ('information_schema','pg_catalog')"
            ).fetchall()
        finally:
            if own:
                con.close()
        return {f"{s}.{v}": sql for s, v, sql in rows}

    def tables(self, con=None, prefix: str = "") -> list[str]:
        own = con is None
        con = con or self.connect()
        try:
            rows = con.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' ORDER BY 1,2"
            ).fetchall()
        finally:
            if own:
                con.close()
        return [f"{s}.{t}" for s, t in rows if t.startswith(prefix) and s != BACKUP_SCHEMA]

    def config_tables(self, con=None) -> dict[str, dict]:
        """{fqn: {"columns": [(name,type)...], "rows": [dict,...]}} for every cfg_* table."""
        own = con is None
        con = con or self.connect()
        out = {}
        try:
            allcols = con.execute(
                "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                "ORDER BY table_schema, table_name, ordinal_position").fetchall()
            colmap: dict[str, list] = {}
            for s_, t_, c_, d_ in allcols:
                colmap.setdefault(f"{s_}.{t_}", []).append((c_, d_))
            for fqn in self.tables(con, prefix="cfg_"):
                s, t = fqn.split(".")
                cols = colmap.get(fqn, [])
                names = [c[0] for c in cols]
                rows = [dict(zip(names, r)) for r in con.execute(f'SELECT * FROM "{s}"."{t}"').fetchall()]
                out[fqn] = {"columns": [list(c) for c in cols], "rows": rows}
        finally:
            if own:
                con.close()
        return out

    def jobs(self) -> dict[str, dict]:
        if not self.jobs_dir.exists():
            return {}
        return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.jobs_dir.glob("*.json"))}

    def bundle(self) -> dict:
        if not self.bundle_path.exists():
            return {}
        return yaml.safe_load(self.bundle_path.read_text(encoding="utf-8")) or {}


def workspaces(root: Path | str | None = None) -> dict[str, Workspace]:
    root = Path(root) if root else DEFAULT_ROOT
    return {e: Workspace(root, e) for e in ENVS}
