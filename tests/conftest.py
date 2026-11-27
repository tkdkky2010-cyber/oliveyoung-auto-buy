import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "phone_auto"))
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmpdb(tmp_path):
    import storage
    old = storage._db_path
    storage.use_db(tmp_path / "test.db")
    yield storage
    storage.use_db(old)
