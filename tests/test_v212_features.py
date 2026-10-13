"""v2.12.0: pictures as first-class media, dupe detector, graveyard, stats,
playlists, redgifs deep data (tags/views/likes)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

from sqlalchemy import inspect

import app.database as db
import app.scanner as scanner
from app.main import app
from app.models import Link, Playlist, PlaylistItem


def _add_link(session, url, **kw):
    kw.setdefault("post_title", "")
    kw.setdefault("status", "sent")
    kw.setdefault("sent_at", datetime.now())
    kw.setdefault("found_at", datetime.now())
    link = Link(url=url, **kw)
    session.add(link)
    session.commit()
    return link.id


# --- pictures are media now ---------------------------------------------------

def test_extract_picks_up_pictures():
    payload = {"data": {"children": [
        {"data": {"url": "https://i.redd.it/a.jpg", "title": "p1"}},
        {"data": {"url": "https://i.imgur.com/b.png", "title": "p2"}},
        {"data": {"url": "https://i.redd.it/c.webp", "title": "p3"}},
        {"data": {"url": "https://i.redd.it/d.jpeg", "title": "p4"}},
        {"data": {"url": "https://example.com/page", "title": "no"}},
    ]}}
    items = scanner.extract_media_links(payload)
    assert [i["url"] for i in items] == [
        "https://i.redd.it/a.jpg",
        "https://i.imgur.com/b.png",
        "https://i.redd.it/c.webp",
        "https://i.redd.it/d.jpeg",
    ]


def test_media_embed_images():
    assert scanner.media_embed("https://i.redd.it/a.jpg") == (
        "image", "https://i.redd.it/a.jpg")
    assert scanner.media_embed("https://i.imgur.com/b.PNG")[0] == "image"
    assert scanner.media_embed("https://i.redd.it/c.webp")[0] == "image"
    assert scanner.media_embed("https://i.redd.it/d.jpeg")[0] == "image"
    assert scanner.media_embed("https://i.redd.it/e.gif")[0] == "image"


def test_media_exts_covers_pictures():
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        assert ext in scanner.MEDIA_EXTS


# --- redgifs deep data ----------------------------------------------------------

def test_redgifs_lookup_returns_details(monkeypatch):
    monkeypatch.setattr(scanner, "_rg_token", "tok")

    def fake_get(url, headers=None, timeout=None):
        resp = Mock()
        resp.status_code = 200
        resp.json.return_value = {"gif": {"userName": "c1",
                                          "tags": ["a", "b"],
                                          "views": 1234, "likes": 56}}
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    alive, size, creator, details = scanner._check_url_alive(
        "https://www.redgifs.com/watch/AbcXyz")
    assert alive is True and creator == "c1"
    assert details == {"tags": ["a", "b"], "views": 1234, "likes": 56}


def test_linkcheck_stores_deep_data(fresh_db, monkeypatch):
    session = db.get_session()
    lid = _add_link(session, "https://www.redgifs.com/watch/AbcXyz")
    session.close()
    monkeypatch.setattr(scanner, "_check_url_alive",
                         lambda url: (True, None, "c1",
                                      {"tags": ["x", "y"], "views": 10, "likes": 2}))
    monkeypatch.setattr(scanner.time, "sleep", lambda s: None)
    assert scanner.run_link_check() is True
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == lid).first()
        assert row.creator == "c1"
        assert row.tags == "x, y" and row.views == 10 and row.likes == 2
    finally:
        session.close()


def test_deep_data_columns_exist(fresh_db):
    cols = [c["name"] for c in inspect(db.get_engine()).get_columns("links")]
    assert "tags" in cols and "views" in cols and "likes" in cols
    pcols = [c["name"] for c in inspect(db.get_engine()).get_columns("playlists")]
    assert "name" in pcols
    picols = [c["name"] for c in inspect(db.get_engine()).get_columns("playlist_items")]
    assert "playlist_id" in picols and "link_id" in picols


# --- dupe detector --------------------------------------------------------------

def test_find_dupe_groups_same_post(fresh_db):
    session = db.get_session()
    _add_link(session, "https://www.redgifs.com/watch/AAA",
              post_permalink="/r/s/comments/abc123/t1/", post_title="T1")
    _add_link(session, "https://v.redd.it/xyz",
              post_permalink="/r/s/comments/abc123/t2/", post_title="T2",
              favorite=True)
    _add_link(session, "https://i.redd.it/solo.jpg",
              post_permalink="/r/s/comments/zzz9/t/", post_title="solo")
    session.close()
    groups = scanner.find_dupe_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["reason"] == "same post" and len(g["links"]) == 2
    winner = next(l for l in g["links"] if l.id == g["winner_id"])
    assert winner.favorite is True  # favorite wins


def test_find_dupe_groups_same_file(fresh_db):
    session = db.get_session()
    _add_link(session, "http://www.redgifs.com/watch/AAA", post_title="a")
    _add_link(session, "https://redgifs.com/watch/AAA", post_title="b")
    session.close()
    groups = scanner.find_dupe_groups()
    assert len(groups) == 1 and groups[0]["reason"] == "same file"


def test_no_dupes_no_groups(fresh_db):
    session = db.get_session()
    _add_link(session, "https://www.redgifs.com/watch/AAA",
              post_permalink="/r/s/comments/abc123/t/")
    _add_link(session, "https://www.redgifs.com/watch/BBB",
              post_permalink="/r/s/comments/def456/t/")
    session.close()
    assert scanner.find_dupe_groups() == []


def test_merge_dupe_group(fresh_db):
    session = db.get_session()
    w = _add_link(session, "https://www.redgifs.com/watch/AAA",
                  post_permalink="/r/s/comments/abc123/t/", favorite=True)
    _add_link(session, "https://v.redd.it/xyz",
              post_permalink="/r/s/comments/abc123/t/")
    session.close()
    groups = scanner.find_dupe_groups()
    assert len(groups) == 1
    deleted = scanner.merge_dupe_group(groups[0]["winner_id"],
                                       [l.id for l in groups[0]["links"]])
    assert deleted == 1
    session = db.get_session()
    try:
        assert [r.id for r in session.query(Link).all()] == [w]
    finally:
        session.close()


def test_dupes_page_and_merge(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://www.redgifs.com/watch/AAA",
              post_permalink="/r/s/comments/abc123/t/", post_title="Dupe A")
    _add_link(session, "https://v.redd.it/xyz",
              post_permalink="/r/s/comments/abc123/t/", post_title="Dupe B")
    session.close()
    with TestClient(app) as client:
        r = client.get("/dupes")
        assert r.status_code == 200 and "Dupe group 1" in r.text
        r = client.post("/dupes/merge-all")
        assert r.status_code in (200, 303)
    session = db.get_session()
    try:
        assert session.query(Link).count() == 1
    finally:
        session.close()


# --- graveyard ------------------------------------------------------------------

def test_graveyard_lists_and_purges(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://i.redd.it/dead.jpg", alive=False,
              post_title="Dead pic")
    _add_link(session, "https://i.redd.it/live.jpg", alive=True,
              post_title="Live pic")
    session.close()
    with TestClient(app) as client:
        r = client.get("/graveyard")
        assert r.status_code == 200
        assert "Dead pic" in r.text and "Live pic" not in r.text
        r = client.post("/graveyard/purge")
        assert r.status_code in (200, 303)
    session = db.get_session()
    try:
        rows = session.query(Link).all()
        assert len(rows) == 1 and rows[0].post_title == "Live pic"
    finally:
        session.close()


# --- playlists ------------------------------------------------------------------

def test_playlists_crud(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    lid = _add_link(session, "https://i.redd.it/x.jpg", post_title="Pic")
    session.close()
    with TestClient(app) as client:
        r = client.post("/playlists", data={"name": "My list"})
        assert r.status_code in (200, 303)
        r = client.get("/playlists")
        assert "My list" in r.text
        session = db.get_session()
        pid = session.query(Playlist).first().id
        session.close()

        r = client.post(f"/api/playlists/{pid}/add/{lid}")
        assert r.json()["ok"] is True
        r = client.post(f"/api/playlists/{pid}/add/{lid}")  # idempotent
        assert r.json()["ok"] is True
        session = db.get_session()
        try:
            assert session.query(PlaylistItem).count() == 1
        finally:
            session.close()

        r = client.get(f"/playlists/{pid}")
        assert r.status_code == 200 and "Pic" in r.text

        client.post(f"/api/playlists/{pid}/remove/{lid}")
        r = client.get(f"/playlists/{pid}")
        assert "Pic" not in r.text

        client.post(f"/playlists/{pid}/delete")
        session = db.get_session()
        try:
            assert session.query(Playlist).count() == 0
            assert session.query(Link).count() == 1  # link survives
        finally:
            session.close()


# --- stats ----------------------------------------------------------------------

def test_stats_page(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://i.redd.it/a.jpg", post_title="P", author="ann",
              subreddit="pics", favorite=True)
    _add_link(session, "https://v.redd.it/b/x.mp4", post_title="V", author="bob",
              subreddit="vids", duration=125, views=1000)
    session.close()
    with TestClient(app) as client:
        r = client.get("/stats")
        assert r.status_code == 200
        assert "u/ann" in r.text and "r/vids" in r.text
        assert "2m 5s" in r.text  # 125s playtime


def test_stats_nav_and_media_tools(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://i.redd.it/a.jpg", post_title="P")
    session.add(Playlist(name="L", created_at=datetime.now()))
    session.commit()
    session.close()
    with TestClient(app) as client:
        r = client.get("/media")
        assert r.status_code == 200
        assert 'id="lb-pl"' in r.text and "＋ List" in r.text
        assert "/dupes" in r.text and "/graveyard" in r.text
        assert ">Stats<" in r.text and ">Playlists<" in r.text
