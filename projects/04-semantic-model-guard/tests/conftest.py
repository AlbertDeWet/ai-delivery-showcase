import os
import shutil
import tempfile
from pathlib import Path

# marker-signing key for the test session lives in a temp folder, not in the user's home
os.environ.setdefault("SMGUARD_KEY_DIR", tempfile.mkdtemp(prefix="smguard-key-"))

import pytest

from smguard import generate as gen


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    """One synthetic generation shared by tests (read-only; copy before mutating)."""
    root = tmp_path_factory.mktemp("gen")
    gen.generate(root)
    return root


@pytest.fixture
def workspace(generated, tmp_path):
    """A private mutable copy of the generated models + reports (data is shared)."""
    for sub in ("models", "reports"):
        shutil.copytree(generated / sub, tmp_path / sub)
    return tmp_path


@pytest.fixture(scope="session")
def demo_summary(tmp_path_factory):
    from smguard.demo import run
    root = tmp_path_factory.mktemp("demo")
    return root, run(root)


def data_dir(generated: Path, env: str) -> Path:
    return generated / "data" / env / "v1"
