"""SQLite database setup, session factory, and sent_links.txt migration.

The DB lives at $REDSAVES_DATA_DIR/redsaves.db (default /data/redsaves.db,
mounted as a Docker volume). On startup, if the links table is empty, any
existing sent_links.txt (in the data dir or next to the legacy script) is
migrated in so history survives the move to the container.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.models import Base, Link

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("REDSAVES_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "redsaves.db"

_engine = None
SessionLocal = None


def get_engine():
    global _engine, SessionLocal
    if _engine is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
        SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)
    return _engine


def get_session():
    get_engine()
    return SessionLocal()


def init_db() -> None:
    """Create tables and migrate legacy sent_links.txt on first run."""
    get_engine()
    Base.metadata.create_all(_engine)
    _ensure_author_column()
    _ensure_source_column()
    _ensure_favorite_column()
    migrate_legacy_history()


def _ensure_author_column() -> None:
    """Add links.author to databases created before v2.6.0 (no Alembic needed;
    this is the only schema change so far)."""
    cols = [c["name"] for c in inspect(_engine).get_columns("links")]
    if "author" not in cols:
        with _engine.begin() as conn:
            conn.execute(text("ALTER TABLE links ADD COLUMN author VARCHAR(128) DEFAULT ''"))
        logger.info("added links.author column")


def _ensure_source_column() -> None:
    """Add links.source to databases created before v2.7.0 (same no-Alembic
    guard pattern as _ensure_author_column)."""
    cols = [c["name"] for c in inspect(_engine).get_columns("links")]
    if "source" not in cols:
        with _engine.begin() as conn:
            conn.execute(text("ALTER TABLE links ADD COLUMN source VARCHAR(16) DEFAULT 'saved'"))
        logger.info("added links.source column")


def _ensure_favorite_column() -> None:
    """Add links.favorite to databases created before v2.9.0 (same no-Alembic
    guard pattern)."""
    cols = [c["name"] for c in inspect(_engine).get_columns("links")]
    if "favorite" not in cols:
        with _engine.begin() as conn:
            conn.execute(text("ALTER TABLE links ADD COLUMN favorite BOOLEAN DEFAULT 0"))
        logger.info("added links.favorite column")


def migrate_legacy_history() -> int:
    """Import sent_links.txt URLs as already-sent links. Returns count added."""
    session = get_session()
    try:
        if session.query(Link).count() > 0:
            return 0  # already migrated (or already in use)
        candidates = [
            DATA_DIR / "sent_links.txt",
            Path(__file__).resolve().parent.parent / "sent_links.txt",
        ]
        migrated = 0
        for path in candidates:
            if not path.exists():
                continue
            for line in path.read_text().splitlines():
                url = line.strip()
                if not url:
                    continue
                if session.query(Link).filter(Link.url == url).first():
                    continue
                session.add(Link(url=url, status="sent", sent_at=datetime.now()))
                migrated += 1
            logger.info("migrated %d URLs from %s", migrated, path)
            break  # first file found wins; don't double-import
        session.commit()
        return migrated
    finally:
        session.close()
