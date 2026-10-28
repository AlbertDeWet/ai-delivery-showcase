import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nl2sql import executor, synth  # noqa: E402


@pytest.fixture(scope="session")
def db_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "contoso.duckdb"
    synth.build_database(path)
    return path


@pytest.fixture()
def con(db_path):
    c = executor.connect(db_path)
    yield c
    c.close()
