"""v2.9.0: media center — grid, lightbox, filters, favorites."""

from __future__ import annotations

from datetime import datetime

import pytest

import app.database as db
import app.scanner as scanner
from app.main import app
from app.models import Link


def _add_link(session, url, title="", author="", subreddit="", fav=False):
    link = Link(url=url, post_title=title, post_permalink="", subreddit=subreddit,
                author=author, status="sent", sent_at=datetime.now(),
                found_at=datetime.now(), favorite=fav)
    session.add(link)
    session.commit()
    return link.id


def test_media_embed_kinds():
    assert scanner.media_embed("https://www.redgifs.com/watch/AbcXyz") == (
        "redgifs", "https://www.redgifs.com/ifr/AbcXyz")
    assert scanner.media_embed("https://v3.redgifs.com/watch/AbcXyz") == (
        "redgifs", "https://www.redgifs.com/ifr/AbcXyz")
    assert scanner.media_embed("https://www.redgifs.com/ifr/AbcXyz") == (
        "redgifs", "https://www.redgifs.com/ifr/AbcXyz")
    assert scanner.media_embed("https://v.redd.it/abc123/CMAF_1080.mp4") == (
        "video", "https://v.redd.it/abc123/CMAF_1080.mp4")
    assert scanner.media_embed("https://i.redd.it/x1.gif") == (
        "image", "https://i.redd.it/x1.gif")
    assert scanner.media_embed("https://i.imgur.com/x1.mp4") == (
        "video", "https://i.imgur.com/x1.mp4")
    assert scanner.media_embed("https://example.com/page")[0] == "link"


def test_favorite_column_migrated(fresh_db):
    session = db.get_session()
    try:
        link_id = _add_link(session, "https://v.redd.it/a1/x.mp4")
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.favorite is False
    finally:
        session.close()


def test_media_page_renders_grid(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://www.redgifs.com/watch/AbcXyz",
               title="Nice clip", author="poster1", subreddit="sub1")
    _add_link(session, "https://v.redd.it/a1/CMAF_1080.mp4", author="poster2")
    session.close()
    with TestClient(app) as client:
        r = client.get("/media")
        assert r.status_code == 200
        assert "Nice clip" in r.text
        assert "www.redgifs.com/ifr/AbcXyz" in r.text
        assert "poster1" in r.text  # author filter dropdown


def test_media_filters(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://v.redd.it/a1/x.mp4", title="Alpha", author="amy")
    _add_link(session, "https://v.redd.it/b2/x.mp4", title="Beta", author="bob")
    session.close()
    with TestClient(app) as client:
        r = client.get("/media", params={"author": "amy"})
        assert r.status_code == 200
        assert "Alpha" in r.text and "Beta" not in r.text
        r = client.get("/media", params={"q": "beta"})
        assert "Beta" in r.text and "Alpha" not in r.text


def test_favorite_toggle(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    link_id = _add_link(session, "https://v.redd.it/a1/x.mp4", title="Fav me")
    session.close()
    with TestClient(app) as client:
        r = client.post(f"/api/favorite/{link_id}")
        assert r.status_code == 200 and r.json()["favorite"] is True
        r = client.get("/media", params={"fav": 1})
        assert "Fav me" in r.text
        r = client.post(f"/api/favorite/{link_id}")
        assert r.json()["favorite"] is False
        r = client.post("/api/favorite/999999")
        assert r.status_code == 404
