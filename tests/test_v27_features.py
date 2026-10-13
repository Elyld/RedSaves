"""v2.7.0 features: configurable post limit + upvoted-posts source."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

import app.database as db
import app.scanner as scanner
from app.models import Link, Run
from tests.conftest import saved_posts_json


def _item(url, **kw):
    d = {"url": url, "title": "t", "permalink": "/r/x/comments/1",
         "subreddit": "x", "author": "someone"}
    d.update(kw)
    return d


# --- post_limit setting ------------------------------------------------------
def test_post_limit_defaults_and_clamps(fresh_db):
    assert scanner.get_post_limit() == 25
    scanner.set_setting_value("post_limit", "999")
    assert scanner.get_post_limit() == 100
    scanner.set_setting_value("post_limit", "0")
    assert scanner.get_post_limit() == 1
    scanner.set_setting_value("post_limit", "-5")
    assert scanner.get_post_limit() == 1
    scanner.set_setting_value("post_limit", "garbage")
    assert scanner.get_post_limit() == 25
    scanner.set_setting_value("post_limit", "50")
    assert scanner.get_post_limit() == 50


def test_set_post_limit_returns_stored_int(fresh_db):
    assert scanner.set_post_limit("1000") == 100
    assert scanner.set_post_limit("abc") == 25
    assert scanner.set_post_limit(7) == 7


def test_limit_passed_as_query_param(fresh_db, monkeypatch, config):
    calls = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["url"] = url
        calls["params"] = params
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = saved_posts_json([])
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    assert scanner.fetch_saved_posts(config, "tok", 50) == {"data": {"children": []}}
    assert calls["url"].endswith("/user/someuser/saved")
    assert calls["params"] == {"limit": 50}


def test_fetch_defaults_to_setting_limit(fresh_db, monkeypatch, config):
    scanner.set_post_limit(42)
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["params"] = params
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = saved_posts_json([])
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    scanner.fetch_saved_posts(config, "tok")
    assert seen["params"] == {"limit": 42}


# --- upvoted source: merge + dedup -------------------------------------------
def test_extract_tags_origin(fresh_db):
    (got,) = scanner.extract_media_links(
        saved_posts_json([_item("https://i.imgur.com/a.gif")]), origin="upvoted")
    assert got["source"] == "upvoted"
    (got2,) = scanner.extract_media_links(
        saved_posts_json([_item("https://i.imgur.com/a.gif")]))
    assert got2["source"] == "saved"  # default preserved for old callers


def test_merge_sources_cross_dedup():
    saved = scanner.extract_media_links(saved_posts_json([
        _item("https://i.imgur.com/a.gif"), _item("https://i.imgur.com/b.gif"),
    ]), origin="saved")
    upvoted = scanner.extract_media_links(saved_posts_json([
        _item("https://i.imgur.com/b.gif"), _item("https://i.imgur.com/c.gif"),
    ]), origin="upvoted")
    merged = scanner.merge_sources(saved, upvoted)
    assert [m["url"] for m in merged] == [
        "https://i.imgur.com/a.gif",
        "https://i.imgur.com/b.gif",
        "https://i.imgur.com/c.gif",
    ]
    assert [m["source"] for m in merged] == ["saved", "both", "upvoted"]


def test_mark_links_upgrades_source_to_both(fresh_db):
    session = db.get_session()
    try:
        session.add(Link(url="https://i.imgur.com/a.gif", status="sent",
                         source="saved"))
        session.commit()
    finally:
        session.close()
    scanner.mark_links([{"url": "https://i.imgur.com/a.gif", "source": "upvoted",
                         "author": "u2"}], "sent", 1)
    session = db.get_session()
    try:
        row = session.query(Link).filter(
            Link.url == "https://i.imgur.com/a.gif").one()
        assert row.source == "both"
        assert row.author == "u2"
    finally:
        session.close()


# --- upvoted fetch: 403 degradation ------------------------------------------
def _fake_reddit(monkeypatch, saved_items, upvoted_status):
    def fake_get(url, params=None, headers=None, timeout=None):
        resp = Mock()
        if url.endswith("/saved"):
            resp.status_code = 200
            resp.json.return_value = saved_posts_json(saved_items)
        else:  # /upvoted
            resp.status_code = upvoted_status
            resp.json.return_value = {}
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(scanner, "send_to_jdownloader", lambda cfg, urls: len(urls))
    monkeypatch.setattr(scanner, "send_pushover_notification",
                        lambda *a, **k: None)


def test_upvoted_403_keeps_saved_and_sets_warning(fresh_db, monkeypatch, config, tmp_path):
    scanner.set_setting_value("include_upvoted", "1")
    _fake_reddit(monkeypatch, [_item("https://i.imgur.com/a.gif")], 403)
    run_id = scanner.run_scan(config)
    assert run_id is not None
    session = db.get_session()
    try:
        run = session.query(Run).filter(Run.id == run_id).one()
        assert run.status == "ok"  # saved source carried the run
        assert run.links_sent == 1
        (link,) = session.query(Link).all()
        assert link.source == "saved"
    finally:
        session.close()
    assert scanner.upvoted_scope_missing() is True
    log = (db.DATA_DIR / "redsaves.log").read_text()
    assert "Reddit token lacks the history scope" in log


def test_upvoted_success_clears_warning_and_merges(fresh_db, monkeypatch, config):
    scanner.set_setting_value("include_upvoted", "1")
    scanner.set_upvoted_scope_missing(True)  # stale flag from an earlier failure

    def fake_get(url, params=None, headers=None, timeout=None):
        resp = Mock()
        resp.status_code = 200
        if url.endswith("/saved"):
            resp.json.return_value = saved_posts_json(
                [_item("https://i.imgur.com/a.gif")])
        else:
            resp.json.return_value = saved_posts_json(
                [_item("https://i.imgur.com/a.gif"),
                 _item("https://i.imgur.com/b.mp4")])
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(scanner, "send_to_jdownloader", lambda cfg, urls: len(urls))
    monkeypatch.setattr(scanner, "send_pushover_notification",
                        lambda *a, **k: None)
    run_id = scanner.run_scan(config)
    assert scanner.upvoted_scope_missing() is False
    session = db.get_session()
    try:
        rows = {r.url: r.source for r in session.query(Link).all()}
        assert rows == {"https://i.imgur.com/a.gif": "both",
                        "https://i.imgur.com/b.mp4": "upvoted"}
    finally:
        session.close()


def test_upvoted_off_preserves_old_behavior(fresh_db, monkeypatch, config):
    # default off: /upvoted is never even requested
    requested = []

    def fake_get(url, params=None, headers=None, timeout=None):
        requested.append(url)
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = saved_posts_json(
            [_item("https://i.imgur.com/a.gif")])
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(scanner, "send_to_jdownloader", lambda cfg, urls: len(urls))
    monkeypatch.setattr(scanner, "send_pushover_notification",
                        lambda *a, **k: None)
    scanner.run_scan(config)
    assert requested and all(u.endswith("/saved") for u in requested)


# --- web: settings roundtrip + banner ----------------------------------------
def test_settings_saves_limit_and_upvoted(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        html = client.get("/settings").text
        assert 'name="post_limit"' in html
        assert 'name="include_upvoted"' in html
        r = client.post("/settings", data={
            "scan_time_1": "08:00", "scan_time_2": "20:00",
            "post_limit": "500", "include_upvoted": "1",
        })
        assert r.status_code in (200, 303)
        assert scanner.get_post_limit() == 100  # clamped server-side
        assert scanner.include_upvoted() is True
        # unchecking the box posts nothing -> off
        client.post("/settings", data={
            "scan_time_1": "08:00", "scan_time_2": "20:00",
            "post_limit": "10",
        })
        assert scanner.include_upvoted() is False
        assert scanner.get_post_limit() == 10


def test_dashboard_warning_banner_and_dismiss(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    scanner.set_upvoted_scope_missing(True)
    with TestClient(app) as client:
        html = client.get("/").text
        assert "Upvoted posts aren't being scanned" in html
        assert "history" in html and "re-authorize" in html
        r = client.post("/api/dismiss-upvoted-warning")
        assert r.status_code in (200, 303)
        assert scanner.upvoted_scope_missing() is False
        assert "Upvoted posts aren't being scanned" not in client.get("/").text
