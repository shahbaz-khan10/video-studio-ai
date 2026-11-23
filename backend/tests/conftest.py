import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="cinema-ai-dub-tests-"))
_DATA_DIR = _TEST_ROOT / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

os.environ["DATA_DIR"] = str(_DATA_DIR)
os.environ["DATABASE_URL"] = f"sqlite:///{(_DATA_DIR / 'test.db').as_posix()}"
os.environ["MAX_UPLOAD_MB"] = "16"
os.environ["UPLOAD_CHUNK_MB"] = "1"
os.environ["ENVIRONMENT"] = "test"


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_root():
    yield
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.database import Base, engine, init_db
    from app.main import app

    Base.metadata.drop_all(bind=engine)
    artifacts = settings.data_dir / "artifacts"
    if artifacts.exists():
        shutil.rmtree(artifacts, ignore_errors=True)
    init_db()

    with TestClient(app) as test_client:
        yield test_client

    Base.metadata.drop_all(bind=engine)
