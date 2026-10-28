import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deltarecover.generate import generate  # noqa: E402


@pytest.fixture(scope="session")
def small_ds():
    return generate(seed=7, n_accounts=12, n_months=4, max_txn=4)
