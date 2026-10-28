"""Central configuration: paths, model tiers, limits and the price table."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = Path(os.environ.get("NL2SQL_DB", DATA_DIR / "contoso.duckdb"))
EVAL_FILE = REPO_ROOT / "evals" / "questions.yaml"
RESULTS_DIR = REPO_ROOT / "results"

SEED = 20261023

# Hard cap on rows returned by any generated query (enforced by the guard).
MAX_ROWS = 1000
# Per-query execution timeout in seconds (best effort; DuckDB interrupt).
QUERY_TIMEOUT_S = 20
# Resource caps for the executor connection.
MEMORY_LIMIT = "1GB"
THREADS = 2

# Model tiers for the claude-cli provider. Run through the Claude Code CLI
# on the owner's Claude subscription, not through a metered API key.
TIERS = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}

CLAUDE_CLI = os.environ.get(
    "NL2SQL_CLAUDE_CLI",
    str(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")),
)
CLAUDE_CLI_TIMEOUT_S = 180

# Tiers with a known reason they have no result. Shown in the report instead of a number.
NOT_RUN_REASONS = {
    "opus": "Claude Code CLI 2.1.276 < 2.1.280 required",
}
