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


# --- delete cross-checks ----------------------------------------------------------

def test_merge_consolidates_file_pointer(fresh_db):
    session = db.get_session()
    w = _add_link(session, "https://www.redgifs.com/watch/AAA",
                  post_permalink="/r/s/comments/abc123/t/", post_title="T")
    keeper = _add_link(session, "https://v.redd.it/xyz",
                       post_permalink="/r/s/comments/abc123/t/",
                       local_file="vids/x.mp4", favorite=True, score=50,
                       tags="a, b", creator="c1")
    session.close()
    groups = scanner.find_dupe_groups()
    assert len(groups) == 1
    # the file-backed favorite wins outright
    assert groups[0]["winner_id"] == keeper
    assert scanner.merge_dupe_group(groups[0]["winner_id"],
                                    [l.id for l in groups[0]["links"]]) == 1
    session = db.get_session()
    try:
        rows = session.query(Link).all()
        assert len(rows) == 1 and rows[0].id == keeper
        assert rows[0].local_file == "vids/x.mp4"
        assert rows[0].favorite is True
    finally:
        session.close()


def test_merge_transfers_pointer_to_bare_winner(fresh_db):
    # favorite winner has no file pointer; the loser holds the only one
    session = db.get_session()
    w = _add_link(session, "https://www.redgifs.com/watch/AAA",
                  post_permalink="/r/s/comments/abc123/t/",
                  post_title="Titled", favorite=True)
    _add_link(session, "https://v.redd.it/xyz",
              post_permalink="/r/s/comments/abc123/t/",
              local_file="vids/x.mp4", tags="a, b", creator="c1")
    session.close()
    groups = scanner.find_dupe_groups()
    assert len(groups) == 1
    assert groups[0]["winner_id"] == w  # favorite beats file pointer
    scanner.merge_dupe_group(w, [l.id for l in groups[0]["links"]])
    session = db.get_session()
    try:
        rows = session.query(Link).all()
        assert len(rows) == 1 and rows[0].id == w
        assert rows[0].local_file == "vids/x.mp4"  # pointer transferred
        assert rows[0].tags == "a, b" and rows[0].creator == "c1"
    finally:
        session.close()


def test_purge_skips_file_backed_rows(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://i.redd.it/kept1.jpg", alive=False,
              post_title="Kept file", local_file="pics/k.jpg")
    _add_link(session, "https://i.redd.it/kept2.jpg", alive=False,
              post_title="Kept done", jd_status="done")
    _add_link(session, "https://i.redd.it/gone.jpg", alive=False,
              post_title="Gone")
    session.close()
    result = scanner.purge_graveyard()
    assert result == {"deleted": 1, "skipped": 2}
    with TestClient(app) as client:
        r = client.get("/graveyard")
        assert "Kept file" in r.text and "Kept done" in r.text
        assert "Gone" not in r.text
        r = client.post("/graveyard/purge")
        assert r.status_code in (200, 303)
        r = client.get("/graveyard?deleted=1&skipped=2")
        assert "Purged 1 dead link(s)" in r.text
        assert "Kept 2 with downloaded files" in r.text


# --- stash lookup endpoint ----------------------------------------------------------

def test_stash_lookup_exact_local_file(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://www.redgifs.com/watch/AbcXyz",
              post_title="Great clip", author="poster1", creator="creator1",
              subreddit="gifs", flair="OC", tags="funny, loop", score=321,
              post_permalink="/r/gifs/comments/abc123/t/", favorite=True,
              local_file="vids/AbcXyz.mp4",
              sent_at=datetime(2026, 10, 1, 12, 0, 0))
    _add_link(session, "https://i.redd.it/other.jpg", post_title="Other")
    session.close()
    with TestClient(app) as client:
        r = client.get("/api/stash/lookup", params={"filename": "AbcXyz.mp4"})
        assert r.status_code == 200
        d = r.json()
        assert d["found"] is True
        assert d["title"] == "Great clip"
        assert d["performer"] == "creator1"  # creator preferred over author
        assert "gifs" in d["tags"] and "OC" in d["tags"]
        assert "funny" in d["tags"]
        assert d["url"] == "https://www.reddit.com/r/gifs/comments/abc123/t/"
        assert d["rating100"] == 100
        assert d["date"] == "2026-10-01"
        assert "u/poster1" in d["details"]


def test_stash_lookup_key_fallback_and_miss(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://v.redd.it/vid9/DASH_720.mp4",
              post_title="Clip", author="bob")
    session.close()
    with TestClient(app) as client:
        # no local_file, but the v.redd.it id matches the filename stem
        r = client.get("/api/stash/lookup", params={"filename": "vid9.mp4"})
        assert r.json()["found"] is True
        assert r.json()["performer"] == "bob"  # author fallback
        assert r.json()["rating100"] is None
        r = client.get("/api/stash/lookup", params={"filename": "nope.mp4"})
        assert r.json() == {"found": False}
        r = client.get("/api/stash/lookup", params={"filename": ".mp4"})
        assert r.json() == {"found": False}
