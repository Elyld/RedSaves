"""v2.13.0: post flair + upvote ratio capture, JDownloader status sync."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

from sqlalchemy import inspect

import app.database as db
import app.scanner as scanner
from app.main import app
from app.models import Link


def _add_link(session, url, **kw):
    kw.setdefault("post_title", "")
    kw.setdefault("status", "sent")
    kw.setdefault("sent_at", datetime.now())
    kw.setdefault("found_at", datetime.now())
    link = Link(url=url, **kw)
    session.add(link)
    session.commit()
    return link.id


# --- flair + upvote ratio -------------------------------------------------------

def test_extract_captures_flair_and_ratio():
    payload = {"data": {"children": [
        {"data": {"url": "https://i.redd.it/a.jpg", "title": "t",
                  "link_flair_text": "OC", "upvote_ratio": 0.97}},
        {"data": {"url": "https://i.redd.it/b.jpg", "title": "t"}},
    ]}}
    items = scanner.extract_media_links(payload)
    assert items[0]["flair"] == "OC" and items[0]["upvote_ratio"] == 0.97
    assert items[1]["flair"] == "" and items[1]["upvote_ratio"] is None


def test_apply_enrichment_fills_flair_and_ratio(fresh_db):
    session = db.get_session()
    lid = _add_link(session, "https://i.redd.it/a.jpg")
    session.close()
    scanner.apply_enrichment(lid, {"post_title": "T", "flair": "GIF",
                                   "upvote_ratio": 0.83, "score": 5,
                                   "num_comments": 1})
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == lid).first()
        assert row.flair == "GIF" and row.upvote_ratio == 0.83
    finally:
        session.close()


def test_refresh_metadata_updates_ratio(fresh_db):
    session = db.get_session()
    lid = _add_link(session, "https://i.redd.it/a.jpg", post_title="T",
                    upvote_ratio=0.5)
    session.close()
    scanner.refresh_metadata(lid, {"score": 9, "num_comments": 2,
                                   "upvote_ratio": 0.91})
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == lid).first()
        assert row.upvote_ratio == 0.91
    finally:
        session.close()


def test_v213_columns_exist(fresh_db):
    cols = [c["name"] for c in inspect(db.get_engine()).get_columns("links")]
    assert "flair" in cols and "upvote_ratio" in cols and "jd_status" in cols


def test_media_page_shows_flair_and_jd_badges(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://i.redd.it/a.jpg", post_title="Flaired",
              flair="OC", jd_status="failed", upvote_ratio=0.97)
    session.close()
    with TestClient(app) as client:
        r = client.get("/media")
        assert r.status_code == 200
        assert "🏷️ OC" in r.text
        assert "JD failed" in r.text
        assert '"ratio": 0.97' in r.text  # lightbox data (JS formats it)
        assert "/media/sync-jd" in r.text


# --- JDownloader status sync ----------------------------------------------------

class _FakeLinks:
    def __init__(self, links):
        self._links = links

    def query_links(self):
        return self._links


class _FakeDevice:
    def __init__(self, downloads, grabber):
        self.downloads = _FakeLinks(downloads)
        self.linkgrabber = _FakeLinks(grabber)


class _FakeMyjdapi:
    device = None

    def connect(self, email, password):
        pass

    def get_device(self, name):
        return _FakeMyjdapi.device


def _jd_config(monkeypatch):
    cfg = Mock()
    cfg.email = "e"
    cfg.password = "p"
    cfg.device_name = "d"
    monkeypatch.setattr(scanner.ScanConfig, "from_env",
                        classmethod(lambda cls: cfg))
    monkeypatch.setattr(scanner, "Myjdapi", _FakeMyjdapi)


def test_jd_link_state_mapping():
    assert scanner._jd_link_state({"finished": True}) == "done"
    assert scanner._jd_link_state({"status": "Failed"}) == "failed"
    assert scanner._jd_link_state({"bytesLoaded": 10}) == "downloading"
    assert scanner._jd_link_state({}) == "queued"


def test_run_jd_sync_updates_rows(fresh_db, monkeypatch):
    _jd_config(monkeypatch)
    _FakeMyjdapi.device = _FakeDevice(
        downloads=[
            {"url": "https://i.redd.it/a.jpg", "finished": True},
            {"url": "https://cdn.redgifs.com/b.mp4", "bytesLoaded": 42},
        ],
        grabber=[
            # stored as watch page, JD holds the resolved file: key fallback
            {"url": "https://v.redd.it/vid9/DASH_1080.mp4"},
            {"url": "https://i.redd.it/gone.jpg", "status": "Failed"},
        ],
    )
    session = db.get_session()
    done_id = _add_link(session, "https://i.redd.it/a.jpg")
    dl_id = _add_link(session, "https://cdn.redgifs.com/b.mp4")
    key_id = _add_link(session, "https://www.redgifs.com/watch/vid9")
    fail_id = _add_link(session, "https://i.redd.it/gone.jpg")
    lost_id = _add_link(session, "https://i.redd.it/nothing.jpg")
    session.close()
    assert scanner.run_jd_sync() is True
    session = db.get_session()
    try:
        rows = {r.id: r.jd_status for r in session.query(Link).all()}
        assert rows[done_id] == "done"
        assert rows[dl_id] == "downloading"
        assert rows[key_id] == "queued"  # key fallback matched
        assert rows[fail_id] == "failed"
        assert rows[lost_id] == ""  # not in JD: left alone
    finally:
        session.close()


def test_jd_sync_no_device_keeps_rows(fresh_db, monkeypatch):
    _jd_config(monkeypatch)
    _FakeMyjdapi.device = None
    session = db.get_session()
    lid = _add_link(session, "https://i.redd.it/a.jpg")
    session.close()
    assert scanner.run_jd_sync() is True
    session = db.get_session()
    try:
        assert session.query(Link).filter(Link.id == lid).first().jd_status == ""
    finally:
        session.close()


def test_jdsync_status_endpoint(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient
    _jd_config(monkeypatch)
    _FakeMyjdapi.device = _FakeDevice([], [])
    with TestClient(app) as client:
        r = client.get("/api/jdsync-status")
        assert r.status_code == 200 and r.json()["running"] is False
        r = client.post("/media/sync-jd")
        assert r.status_code in (200, 303)
