"""Builder test fixtures. Seeding is slow on Windows (DuckDB file open/close), so seed once and copy."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envalign.seed import seed  # noqa: E402


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("template") / "workspaces"
    seed(root)
    return root


@pytest.fixture
def root(seeded_template, tmp_path):
    dst = tmp_path / "workspaces"
    shutil.copytree(seeded_template, dst)
    return dst


@pytest.fixture
def logdir(tmp_path):
    d = tmp_path / "apply-log"
    d.mkdir()
    return d
