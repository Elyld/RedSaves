"""v2.6.0 features: JDownloader auto-start, post author, Logs page,
Leaderboard page, and sent_links.txt import upload."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

import app.database as db
import app.scanner as scanner
from app.models import Link
from tests.conftest import saved_posts_json


def _fake_jd(monkeypatch, packages):
    """Fake Myjdapi whose linkgrabber reports the given packages."""
    linkgrabber = Mock()
    linkgrabber.query_packages.return_value = packages
    device = Mock()
    device.linkgrabber = linkgrabber
    api = Mock()
    api.get_device.return_value = device
    monkeypatch.setattr(scanner, "Myjdapi", Mock(return_value=api))
    return linkgrabber


# --- JDownloader auto-start --------------------------------------------------
def test_autostart_moves_packages_to_download_list(fresh_db, monkeypatch, config):
    lg = _fake_jd(monkeypatch, [
        {"uuid": "pkg-1", "name": "RedditMedia"},
        {"uuid": "pkg-2", "name": "SomeOtherPackage"},
    ])
    n = scanner.send_to_jdownloader(config, ["https://i.imgur.com/a.gif"])
    assert n == 1
    # add_links still passes autostart (best effort) ...
    add_call = lg.add_links.call_args
    assert add_call[0][0][0]["autostart"] is True
    assert add_call[0][0][0]["packageName"] == "RedditMedia"
    # ... and our package is moved out of LinkGrabber so it starts now
    lg.move_to_downloadlist.assert_called_once_with([], ["pkg-1"])


def test_autostart_no_packages_no_move(fresh_db, monkeypatch, config):
    lg = _fake_jd(monkeypatch, [])
    assert scanner.send_to_jdownloader(config, ["https://i.imgur.com/a.gif"]) == 1
    lg.move_to_downloadlist.assert_not_called()


def test_send_failure_marks_nothing_and_never_moves(fresh_db, monkeypatch, config):
    linkgrabber = Mock()
    linkgrabber.add_links.side_effect = RuntimeError("boom")
    device = Mock()
    device.linkgrabber = linkgrabber
    api = Mock()
    api.get_device.return_value = device
    monkeypatch.setattr(scanner, "Myjdapi", Mock(return_value=api))
    assert scanner.send_to_jdownloader(config, ["https://i.imgur.com/a.gif"]) == 0
    linkgrabber.move_to_downloadlist.assert_not_called()


def test_move_failure_still_counts_as_sent(fresh_db, monkeypatch, config):
    """JD accepted the links (add succeeded) — a failed move must not burn
    them into a retry that would duplicate the grabber entries."""
    lg = _fake_jd(monkeypatch, [{"uuid": "pkg-1", "name": "RedditMedia"}])
    lg.move_to_downloadlist.side_effect = RuntimeError("move failed")
    assert scanner.send_to_jdownloader(config, ["https://i.imgur.com/a.gif"]) == 1


# --- post author -------------------------------------------------------------
def test_extract_captures_author(fresh_db):
    posts = saved_posts_json([{
        "url": "https://i.imgur.com/a.gif", "title": "t",
        "permalink": "/r/x/1", "subreddit": "x", "author": "gifwizard",
    }])
    (got,) = scanner.extract_media_links(posts)
    assert got["author"] == "gifwizard"


def test_mark_links_persists_author(fresh_db):
    items = [{"url": "https://i.imgur.com/a.gif", "post_title": "t",
              "post_permalink": "", "subreddit": "x", "author": "gifwizard"}]
    scanner.mark_links(items, "sent", run_id=None)
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.url == "https://i.imgur.com/a.gif").one()
        assert row.author == "gifwizard"
    finally:
        session.close()


def test_author_column_backfilled_on_old_db(fresh_db, tmp_path):
    """init_db adds links.author to a DB created before v2.6.0."""
    db._engine.dispose()
    # rebuild without the author column
    with db._engine.begin() as conn:
        from sqlalchemy import text
        conn.execute(text("ALTER TABLE links DROP COLUMN author"))
    db.init_db()  # must re-add it, not crash
    cols = [c["name"] for c in
            __import__("sqlalchemy").inspect(db._engine).get_columns("links")]
    assert "author" in cols


def test_history_shows_author(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    session = db.get_session()
    try:
        session.add(Link(url="https://i.imgur.com/a.gif", status="sent",
                         post_title="A", author="gifwizard", subreddit="x",
                         sent_at=datetime.now()))
        session.add(Link(url="https://i.imgur.com/b.gif", status="sent",
                         post_title="B", author="", subreddit="x",
                         sent_at=datetime.now()))
        session.commit()
    finally:
        session.close()
    with TestClient(app) as client:
        html = client.get("/history").text
        assert "u/gifwizard" in html
        assert ">—<" in html  # migrated/unknown authors render as em dash


# --- Logs page ---------------------------------------------------------------
def test_logs_page_and_api(fresh_db, tmp_path, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
    (tmp_path / "redsaves.log").write_text("\n".join(f"line {i}" for i in range(500)))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        assert client.get("/logs").status_code == 200
        assert "auto-refreshes every 15s" in client.get("/logs").text
        data = client.get("/api/logs").json()
        assert len(data["lines"]) == 300
        assert data["lines"][0] == "line 200"   # tail, oldest first
        assert data["lines"][-1] == "line 499"  # newest at bottom


def test_logs_api_missing_file(fresh_db, tmp_path, monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        assert client.get("/api/logs").json() == {"lines": []}


# --- Leaderboard -------------------------------------------------------------
def _seed_links(fresh_db):
    session = db.get_session()
    try:
        now = datetime.now()
        rows = [
            # (subreddit, author, days_ago)
            ("pics", "alice", 1), ("pics", "alice", 2), ("pics", "bob", 3),
            ("gifs", "alice", 1), ("gifs", "carol", 9),
            ("videos", "dave", 60),  # outside the 8-week window
        ]
        for i, (sub, author, days) in enumerate(rows):
            session.add(Link(url=f"https://x/{i}.gif", status="sent",
                             subreddit=sub, author=author,
                             sent_at=now - timedelta(days=days)))
        # a failed link must not count anywhere
        session.add(Link(url="https://x/failed.gif", status="failed",
                         subreddit="pics", author="alice", sent_at=now))
        session.commit()
    finally:
        session.close()


def test_top_subreddits(fresh_db):
    import app.stats as stats
    _seed_links(fresh_db)
    session = db.get_session()
    try:
        assert stats.top_subreddits(session) == [("pics", 3), ("gifs", 2), ("videos", 1)]
    finally:
        session.close()


def test_top_authors(fresh_db):
    import app.stats as stats
    _seed_links(fresh_db)
    session = db.get_session()
    try:
        assert stats.top_authors(session) == [
            ("alice", 3), ("bob", 1), ("carol", 1), ("dave", 1)]
    finally:
        session.close()


def test_links_per_week(fresh_db):
    import app.stats as stats
    _seed_links(fresh_db)
    session = db.get_session()
    try:
        weeks = stats.links_per_week(session, weeks=8)
        assert len(weeks) == 8
        assert sum(c for _, c in weeks) == 5  # 60-day-old link excluded
        assert all(c >= 0 for _, c in weeks)
    finally:
        session.close()


def test_top_page_renders(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    _seed_links(fresh_db)
    with TestClient(app) as client:
        r = client.get("/top")
        assert r.status_code == 200
        assert "r/pics" in r.text and "u/alice" in r.text
        assert "Links sent per week" in r.text


# --- sent_links.txt import upload --------------------------------------------
def test_import_upload_dedupes(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app
    session = db.get_session()
    try:
        session.add(Link(url="https://i.imgur.com/old.gif", status="sent",
                         sent_at=datetime.now()))
        session.commit()
    finally:
        session.close()
    body = ("https://i.imgur.com/old.gif\n"
            "https://i.imgur.com/new1.gif\n"
            "https://i.imgur.com/new1.gif\n"   # dup inside the file
            "\n"                                # blank line ignored
            "https://i.imgur.com/new2.gif\n")
    with TestClient(app) as client:
        r = client.post("/history/import",
                        files={"file": ("sent_links.txt", body, "text/plain")})
        assert r.status_code in (200, 303)
        session = db.get_session()
        try:
            urls = {u for (u,) in session.query(Link.url).all()}
        finally:
            session.close()
        assert urls == {"https://i.imgur.com/old.gif",
                        "https://i.imgur.com/new1.gif",
                        "https://i.imgur.com/new2.gif"}
        # the redirect target reports imported/skipped
        page = client.get("/history?imported=2&skipped=2")
        assert "Imported 2 link(s)" in page.text
        assert "skipped 2 already known" in page.text


def test_import_works_with_existing_rows(fresh_db):
    """Unlike the boot migration, the upload works when the table is non-empty."""
    from fastapi.testclient import TestClient
    from app.main import app
    session = db.get_session()
    try:
        session.add(Link(url="https://i.imgur.com/a.gif", status="sent",
                         sent_at=datetime.now()))
        session.commit()
    finally:
        session.close()
    with TestClient(app) as client:
        r = client.post("/history/import",
                        files={"file": ("s.txt", "https://i.imgur.com/b.gif\n",
                                        "text/plain")})
        assert r.status_code in (200, 303)
        session = db.get_session()
        try:
            assert session.query(Link).count() == 2
            row = session.query(Link).filter(
                Link.url == "https://i.imgur.com/b.gif").one()
            assert row.status == "sent" and row.sent_at is not None
        finally:
            session.close()
