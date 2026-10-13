"""v2.11.0: local playback — match JDownloader downloads, serve from disk."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import app.database as db
import app.scanner as scanner
from app.main import app
from app.models import Link


def _add_link(session, url, **kw):
    kw.setdefault("status", "sent")
    kw.setdefault("sent_at", datetime.now())
    kw.setdefault("found_at", datetime.now())
    link = Link(url=url, **kw)
    session.add(link)
    session.commit()
    return link.id


def test_media_keys():
    assert scanner.media_keys("https://www.redgifs.com/watch/AbcXyz123") == ["abcxyz123"]
    assert scanner.media_keys("https://v3.redgifs.com/ifr/AbcXyz123") == ["abcxyz123"]
    assert scanner.media_keys("https://v.redd.it/wlfxk26lsvah1/CMAF_1080.mp4") == ["wlfxk26lsvah1"]
    assert scanner.media_keys("https://i.redd.it/eza5rn1uc1if1.gif") == ["eza5rn1uc1if1"]
    # garbage in -> harmless key that simply won't match any file
    assert scanner.media_keys("not a url") == ["not a url"]


def test_media_embed_prefers_local():
    assert scanner.media_embed("https://www.redgifs.com/watch/Abc",
                               link_id=7, local_file="a.mp4") == ("video", "/media-file/7")
    # no local file -> remote behavior unchanged
    assert scanner.media_embed("https://www.redgifs.com/watch/Abc",
                               link_id=7, local_file="")[0] == "redgifs"


def test_filescan_matches_downloads(fresh_db, tmp_path, monkeypatch):
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "dopeymediumforestgreenamericanredsquirrel.mp4").write_bytes(b"x" * 100)
    (dl / "wlfxk26lsvah1.mp4").write_bytes(b"y" * 50)
    (dl / "unrelated.mp4").write_bytes(b"z" * 10)
    monkeypatch.setenv("MEDIA_DIR", str(dl))

    session = db.get_session()
    id1 = _add_link(session, "https://www.redgifs.com/watch/dopeymediumforestgreenamericanredsquirrel")
    id2 = _add_link(session, "https://v.redd.it/wlfxk26lsvah1/CMAF_1080.mp4")
    id3 = _add_link(session, "https://www.redgifs.com/watch/nomatchhere")
    session.close()

    assert scanner.run_filescan() is True

    session = db.get_session()
    try:
        r1 = session.query(Link).filter(Link.id == id1).first()
        r2 = session.query(Link).filter(Link.id == id2).first()
        r3 = session.query(Link).filter(Link.id == id3).first()
        assert r1.local_file == "dopeymediumforestgreenamericanredsquirrel.mp4"
        assert r2.local_file == "wlfxk26lsvah1.mp4"
        assert not r3.local_file
    finally:
        session.close()
    p = scanner.get_filescan_progress()
    assert p["matched"] == 2 and p["total"] == 3


def test_filescan_missing_dir_leaves_rows_alone(fresh_db, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "does-not-exist"))
    session = db.get_session()
    link_id = _add_link(session, "https://v.redd.it/a1/x.mp4")
    session.close()
    assert scanner.run_filescan() is True
    session = db.get_session()
    try:
        assert session.query(Link).filter(Link.id == link_id).first().local_file in ("", None)
    finally:
        session.close()


def test_media_file_serves_and_blocks_traversal(fresh_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    dl = tmp_path / "downloads"
    dl.mkdir()
    (dl / "a.mp4").write_bytes(b"videobytes")
    monkeypatch.setenv("MEDIA_DIR", str(dl))
    session = db.get_session()
    good = _add_link(session, "https://v.redd.it/a1/x.mp4", local_file="a.mp4")
    evil = _add_link(session, "https://v.redd.it/b2/x.mp4", local_file="../evil.mp4")
    nolocal = _add_link(session, "https://v.redd.it/c3/x.mp4")
    session.close()
    with TestClient(app) as client:
        r = client.get(f"/media-file/{good}")
        assert r.status_code == 200 and r.content == b"videobytes"
        assert client.get(f"/media-file/{evil}").status_code == 403
        assert client.get(f"/media-file/{nolocal}").status_code == 404
        assert client.get("/media-file/999999").status_code == 404


def test_filescan_routes(fresh_db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    dl = tmp_path / "downloads"
    dl.mkdir()
    monkeypatch.setenv("MEDIA_DIR", str(dl))
    with TestClient(app) as client:
        r = client.get("/api/filescan-status")
        assert r.status_code == 200 and r.json()["running"] is False
        r = client.post("/media/scan-files", follow_redirects=False)
        assert r.status_code == 303


def test_media_page_shows_local_badge(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    _add_link(session, "https://www.redgifs.com/watch/Abc",
               post_title="Local clip", local_file="Abc.mp4")
    session.close()
    with TestClient(app) as client:
        r = client.get("/media")
        assert r.status_code == 200
        assert "📁 local" in r.text
        assert "/media-file/" in r.text
