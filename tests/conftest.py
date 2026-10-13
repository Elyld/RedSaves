"""Shared fixtures: isolated SQLite DB per test (REDSAVES_DATA_DIR -> tmp)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Point the data dir at a scratch location BEFORE any app import, so the
# module-level DATA_DIR in app.database never touches /data in tests.
_TEST_ROOT = tempfile.mkdtemp(prefix="redsaves-tests-")
os.environ["REDSAVES_DATA_DIR"] = _TEST_ROOT
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import app.database as db  # noqa: E402
import app.models as models  # noqa: E402
import app.scanner as scanner  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Fresh empty database; module globals repointed at tmp_path."""
    monkeypatch.setenv("REDSAVES_DATA_DIR", str(tmp_path))
    db.DATA_DIR = Path(str(tmp_path))
    db.DB_PATH = db.DATA_DIR / "redsaves.db"
    db._engine = None
    db.SessionLocal = None
    scanner.DATA_DIR = db.DATA_DIR
    scanner.LOG_FILE = db.DATA_DIR / "redsaves.log"
    db.init_db()
    yield db
    if db._engine is not None:
        db._engine.dispose()
    db._engine = None
    db.SessionLocal = None


@pytest.fixture()
def config():
    return scanner.ScanConfig(
        email="jd@example.com", password="pw", device_name="dev",
        client_id="cid", secret_token="sec", refresh_token="ref",
        reddit_username="someuser", destination_folder="/dl",
        pushover_user_key="puk", pushover_api_token="pat",
    )


def saved_posts_json(items):
    """Build a minimal Reddit /saved listing payload."""
    return {"data": {"children": [{"data": it} for it in items]}}
