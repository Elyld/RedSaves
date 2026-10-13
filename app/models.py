"""SQLAlchemy models: sent-link history, scan runs, and key/value settings."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Link(Base):
    """One media link the scanner found. status is 'sent' (JDownloader accepted
    it) or 'failed' (the send failed; it stays eligible for retry)."""

    __tablename__ = "links"

    id = Column(Integer, primary_key=True)
    url = Column(Text, unique=True, nullable=False)
    post_title = Column(Text, default="")
    post_permalink = Column(Text, default="")
    subreddit = Column(String(128), default="")
    author = Column(String(128), default="")
    source = Column(String(16), default="saved")  # saved | upvoted | both
    found_at = Column(DateTime, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    status = Column(String(16), nullable=False, default="sent")
    run_id = Column(Integer, nullable=True)
    favorite = Column(Boolean, nullable=False, default=False)
    # v2.10.0 media-data columns (all auto-migrated, nullable/defaulted)
    score = Column(Integer, nullable=False, default=0)
    num_comments = Column(Integer, nullable=False, default=0)
    thumbnail = Column(Text, default="")
    duration = Column(Integer, nullable=True)  # seconds, when known
    domain = Column(String(64), default="")
    creator = Column(String(128), default="")  # redgifs creator profile
    alive = Column(Boolean, nullable=False, default=True)
    checked_at = Column(DateTime, nullable=True)
    file_size = Column(Integer, nullable=True)  # bytes, when known
    # v2.11.0: local playback — path of the downloaded file, relative to
    # MEDIA_DIR (Kansas blocks redgifs embeds; disk never does)
    local_file = Column(Text, default="")


class Run(Base):
    """One scan execution: scheduled or manual."""

    __tablename__ = "runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="ok")
    # ok | no_new | auth_failed | fetch_failed | send_failed | running
    links_found = Column(Integer, default=0)
    links_new = Column(Integer, default=0)
    links_sent = Column(Integer, default=0)
    error = Column(Text, default="")


class Setting(Base):
    """Key/value settings (scan times, notification toggles). Secrets stay in
    env vars and are never stored here."""

    __tablename__ = "settings"

    key = Column(String(128), primary_key=True)
    value = Column(Text, default="")
