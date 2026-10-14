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


def test_media_embed_local_image(fresh_db):
    """A downloaded picture must play as an image, not a video tag."""
    assert scanner.media_embed("https://www.redgifs.com/watch/Abc",
                               7, "vids/clip.jpg") == ("image", "/media-file/7")
    assert scanner.media_embed("https://v.redd.it/a/x.mp4",
                               8, "vids/clip.mp4") == ("video", "/media-file/8")
    assert scanner.is_picture(url="https://i.redd.it/x1.png")
    assert scanner.is_picture(local_file="vids/x.webp")
    assert not scanner.is_picture(url="https://www.redgifs.com/watch/Abc")


def _add_link_full(session, url, local_file="", title="t"):
    link = Link(url=url, post_title=title, post_permalink="", status="sent",
                sent_at=datetime.now(), found_at=datetime.now(),
                local_file=local_file)
    session.add(link)
    session.commit()
    return link.id


def test_media_pagination(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    for n in range(55):
        _add_link_full(session, f"https://v.redd.it/v{n}/x.mp4", title=f"clip {n}")
    session.close()
    with TestClient(app) as client:
        r1 = client.get("/media")
        assert r1.status_code == 200
        assert r1.text.count('<article class="mcard') == 48
        assert "Showing 1–48 of 55" in r1.text
        assert "Next »" in r1.text
        r2 = client.get("/media", params={"page": 2})
        assert r2.text.count('<article class="mcard') == 7
        assert "Showing 49–55 of 55" in r2.text
        assert "« Prev" in r2.text
        # page beyond the end clamps to the last page
        r3 = client.get("/media", params={"page": 99})
        assert "Showing 49–55 of 55" in r3.text


def test_media_kind_filter(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link_full(session, "https://www.redgifs.com/watch/Abc", title="rg clip")
    _add_link_full(session, "https://v.redd.it/a1/x.mp4", title="redd clip")
    _add_link_full(session, "https://i.redd.it/p1.jpg", title="pic one")
    _add_link_full(session, "https://i.imgur.com/p2.png", title="pic two")
    _add_link_full(session, "https://v.redd.it/a2/x.mp4", title="local pic",
                   local_file="vids/local.jpg")
    session.close()
    with TestClient(app) as client:
        rp = client.get("/media", params={"kind": "pics"})
        assert rp.text.count('<article class="mcard') == 3
        assert "pic one" in rp.text and "local pic" in rp.text
        assert "rg clip" not in rp.text
        rv = client.get("/media", params={"kind": "vids"})
        assert rv.text.count('<article class="mcard') == 2
        assert "rg clip" in rv.text and "pic one" not in rv.text


def test_shuffle_api_respects_filters(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    id_clip = _add_link_full(session, "https://www.redgifs.com/watch/Abc",
                             title="clip")
    id_pic = _add_link_full(session, "https://i.redd.it/p1.jpg", title="pic")
    id_dead = _add_link_full(session, "https://example.com/page", title="page")
    session.close()
    with TestClient(app) as client:
        r = client.get("/api/media/shuffle")
        body = r.json()
        assert r.status_code == 200
        assert set(body["ids"]) == {id_clip, id_pic}  # unplayable excluded
        assert body["count"] == 2
        r2 = client.get("/api/media/shuffle", params={"kind": "pics"})
        assert r2.json()["ids"] == [id_pic]
        r3 = client.get("/api/media/shuffle", params={"q": "clip"})
        assert r3.json()["ids"] == [id_clip]


def test_media_item_api(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    lid = _add_link_full(session, "https://i.redd.it/p1.jpg", title="pic",
                         local_file="")
    session.close()
    with TestClient(app) as client:
        r = client.get(f"/api/media/item/{lid}")
        assert r.status_code == 200
        body = r.json()
        assert body["kind"] == "image" and body["title"] == "pic"
        assert body["src"] == "https://i.redd.it/p1.jpg"
        assert client.get("/api/media/item/999999").status_code == 404
