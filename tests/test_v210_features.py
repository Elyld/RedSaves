"""v2.10.0: media-data collection — thumbnails, scores, durations, dead-link check."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest

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


def _post(url="https://v.redd.it/abc123", title="T", score=123, comments=45,
          thumb="https://b.thumbs.redditmedia.com/x.jpg", duration=61,
          domain="v.redd.it"):
    return {"url": url, "title": title, "permalink": "/r/s/comments/1",
            "subreddit": "s", "author": "u", "score": score,
            "num_comments": comments, "thumbnail": thumb, "domain": domain,
            "created_utc": 1700000000,
            "preview": {"images": [{"source": {"url": "https://preview.redd.it/y.jpg"}}]},
            "media": {"reddit_video": {"duration": duration}}}


def test_post_thumbnail_prefers_preview():
    assert scanner._post_thumbnail(_post()) == "https://preview.redd.it/y.jpg"
    assert scanner._post_thumbnail(_post(thumb="self") | {"preview": {}}) == ""
    p = _post()
    p["preview"]["images"][0]["source"]["url"] = "https://a.b/c.jpg?x=1&amp;y=2"
    assert scanner._post_thumbnail(p) == "https://a.b/c.jpg?x=1&y=2"


def test_post_duration():
    assert scanner._post_duration(_post()) == 61
    assert scanner._post_duration({}) is None


def test_extract_captures_free_metadata(fresh_db):
    payload = {"data": {"children": [{"data": _post()}] }}
    items = scanner.extract_media_links(payload)
    assert len(items) == 1
    it = items[0]
    assert it["score"] == 123
    assert it["num_comments"] == 45
    assert it["thumbnail"] == "https://preview.redd.it/y.jpg"
    assert it["duration"] == 61
    assert it["domain"] == "v.redd.it"
    assert it["created_utc"] == 1700000000


def test_mark_links_writes_and_refreshes_metadata(fresh_db):
    session = db.get_session()
    payload = {"data": {"children": [{"data": _post()}]}}
    items = scanner.extract_media_links(payload)
    scanner.mark_links(items, "sent", run_id=1)
    row = session.query(Link).filter(Link.url == "https://v.redd.it/abc123").first()
    assert row.score == 123 and row.thumbnail == "https://preview.redd.it/y.jpg"
    assert row.duration == 61 and row.domain == "v.redd.it"
    # seen again with new score -> refreshed
    payload2 = {"data": {"children": [{"data": _post(score=999)}]}}
    scanner.mark_links(scanner.extract_media_links(payload2), "sent", run_id=2)
    session.refresh(row)
    assert row.score == 999
    session.close()


def test_apply_enrichment_fills_media_data(fresh_db):
    session = db.get_session()
    link_id = _add_link(session, "https://v.redd.it/abc123")
    session.close()
    scanner.apply_enrichment(link_id, {
        "post_title": "T", "post_permalink": "/p", "subreddit": "s", "author": "u",
        "score": 50, "num_comments": 6, "thumbnail": "https://t.jpg",
        "duration": 30, "domain": "v.redd.it", "created_utc": 1700000000})
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.thumbnail == "https://t.jpg"
        assert row.duration == 30 and row.score == 50
    finally:
        session.close()


def test_refresh_metadata_never_touches_title(fresh_db):
    session = db.get_session()
    link_id = _add_link(session, "https://v.redd.it/abc123",
                        post_title="Original title")
    session.close()
    scanner.refresh_metadata(link_id, {
        "post_title": "HACK", "thumbnail": "https://new.jpg",
        "score": 77, "num_comments": 8, "duration": 42, "domain": "v.redd.it",
        "author": "", "subreddit": "", "post_permalink": ""})
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == "Original title"
        assert row.thumbnail == "https://new.jpg" and row.score == 77
    finally:
        session.close()


def test_check_url_alive_head(monkeypatch):
    def fake_head(url, timeout=None, allow_redirects=None, headers=None):
        resp = Mock()
        resp.status_code = 200
        resp.headers = {"Content-Length": "12345"}
        return resp
    monkeypatch.setattr(scanner.requests, "head", fake_head)
    alive, size, creator, _ = scanner._check_url_alive("https://i.redd.it/x.gif")
    assert alive is True and size == 12345 and creator is None


def test_check_url_alive_404(monkeypatch):
    def fake_head(url, timeout=None, allow_redirects=None, headers=None):
        resp = Mock()
        resp.status_code = 404
        resp.headers = {}
        return resp
    monkeypatch.setattr(scanner.requests, "head", fake_head)
    alive, size, _, _ = scanner._check_url_alive("https://i.redd.it/gone.gif")
    assert alive is False and size is None


def test_redgifs_lookup_alive_and_creator(monkeypatch):
    monkeypatch.setattr(scanner, "_rg_token", "tok")
    def fake_get(url, headers=None, timeout=None):
        resp = Mock()
        assert url == "https://api.redgifs.com/v2/gifs/AbcXyz"
        resp.status_code = 200
        resp.json.return_value = {"gif": {"userName": "creator1"}}
        return resp
    monkeypatch.setattr(scanner.requests, "get", fake_get)
    alive, size, creator, _ = scanner._check_url_alive("https://www.redgifs.com/watch/AbcXyz")
    assert alive is True and creator == "creator1"


def test_redgifs_lookup_404_dead(monkeypatch):
    monkeypatch.setattr(scanner, "_rg_token", "tok")
    def fake_get(url, headers=None, timeout=None):
        resp = Mock()
        resp.status_code = 404
        resp.json.return_value = {}
        return resp
    monkeypatch.setattr(scanner.requests, "get", fake_get)
    alive, _, _, _ = scanner._check_url_alive("https://www.redgifs.com/watch/DeadXyz")
    assert alive is False


def test_redgifs_lookup_api_down_leaves_row_alone(monkeypatch):
    monkeypatch.setattr(scanner, "_rg_token", None)
    def fake_get(url, headers=None, timeout=None):
        raise ConnectionError("nope")
    monkeypatch.setattr(scanner.requests, "get", fake_get)
    alive, _, _, _ = scanner._check_url_alive("https://www.redgifs.com/watch/AbcXyz")
    assert alive is None  # unknown: never false-flag


def test_linkcheck_routes(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://i.redd.it/x.gif")
    session.close()
    monkeypatch.setattr(scanner, "_check_url_alive", lambda url: (True, 10, None, {}))
    monkeypatch.setattr(scanner.time, "sleep", lambda s: None)
    with TestClient(app) as client:
        r = client.get("/api/linkcheck-status")
        assert r.status_code == 200 and r.json()["running"] is False
        # run synchronously for the test
        assert scanner.run_link_check() is True
        session = db.get_session()
        try:
            row = session.query(Link).first()
            assert row.alive is True and row.file_size == 10
            assert row.checked_at is not None
        finally:
            session.close()


def test_media_page_shows_thumbnail_and_badges(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://v.redd.it/a1/x.mp4", post_title="Clip",
               thumbnail="https://thumb.jpg", duration=75, score=321, creator="creator1")
    _add_link(session, "https://v.redd.it/b2/x.mp4", post_title="Dead one", alive=False)
    session.close()
    with TestClient(app) as client:
        r = client.get("/media")
        assert r.status_code == 200
        assert "https://thumb.jpg" in r.text
        assert "1:15" in r.text and "321" in r.text and "creator1" in r.text
        assert "dead" in r.text
        r = client.get("/media", params={"hide_dead": 1})
        assert "Dead one" not in r.text and "Clip" in r.text
        r = client.get("/media", params={"creator": "creator1"})
        assert "Clip" in r.text and "Dead one" not in r.text
