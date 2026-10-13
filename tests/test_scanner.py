"""Behavior-parity tests: the DB-backed scanner must act exactly like
RedSaves2.0.py v2.4. All network calls are mocked; nothing leaves the box."""

from __future__ import annotations

from unittest.mock import Mock

import app.scanner as scanner
from app.models import Link
from tests.conftest import saved_posts_json


def _post(url, title="t", permalink="/r/x/comments/1", subreddit="x"):
    return {"url": url, "title": title, "permalink": permalink,
            "subreddit": subreddit}


# --- media matching ----------------------------------------------------------
def test_media_markers_match_script(fresh_db):
    posts = saved_posts_json([
        _post("https://i.imgur.com/a.gif"),
        _post("https://x.com/v.mp4?x=1"),
        _post("https://redgifs.com/watch/abc"),
        _post("https://v.redd.it/xyz/DASH_720.mp4"),
        _post("https://example.com/photo.jpg"),   # pictures are media too
        _post("https://i.redd.it/x.png"),          # (v2.12.0: jpg/png/webp/jpeg)
        _post("https://example.com/article"),      # not media
        _post(""),                                 # empty url
    ])
    got = scanner.extract_media_links(posts)
    assert [g["url"] for g in got] == [
        "https://i.imgur.com/a.gif",
        "https://x.com/v.mp4?x=1",
        "https://redgifs.com/watch/abc",
        "https://v.redd.it/xyz/DASH_720.mp4",
        "https://example.com/photo.jpg",
        "https://i.redd.it/x.png",
    ]


def test_within_run_dedup_preserves_first_order(fresh_db):
    posts = saved_posts_json([
        _post("https://i.imgur.com/a.gif", title="first"),
        _post("https://i.imgur.com/b.gif", title="second"),
        _post("https://i.imgur.com/a.gif", title="dup"),
    ])
    got = scanner.extract_media_links(posts)
    assert [g["url"] for g in got] == [
        "https://i.imgur.com/a.gif", "https://i.imgur.com/b.gif"]
    assert got[0]["post_title"] == "first"  # first occurrence wins


def test_metadata_captured(fresh_db):
    posts = saved_posts_json([
        _post("https://i.imgur.com/a.gif", title="My Gif",
              permalink="/r/pics/comments/abc/", subreddit="pics"),
    ])
    (got,) = scanner.extract_media_links(posts)
    assert got["post_title"] == "My Gif"
    assert got["post_permalink"] == "/r/pics/comments/abc/"
    assert got["subreddit"] == "pics"


# --- full scan: happy path ---------------------------------------------------
def _mock_net(monkeypatch, posts, jd_result):
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(scanner, "fetch_saved_posts", lambda cfg, tok, limit=None: posts)
    monkeypatch.setattr(scanner, "send_to_jdownloader",
                        lambda cfg, urls: jd_result)


def test_happy_path_marks_sent_and_pings_verbatim(fresh_db, config, monkeypatch):
    posts = saved_posts_json([_post("https://i.imgur.com/a.gif", title="A",
                                    subreddit="pics")])
    _mock_net(monkeypatch, posts, 1)
    pushover = Mock()
    monkeypatch.setattr(scanner, "send_pushover_notification", pushover)

    run_id = scanner.run_scan(config)

    session = fresh_db.get_session()
    try:
        link = session.query(Link).filter(Link.url == "https://i.imgur.com/a.gif").one()
        assert link.status == "sent"
        assert link.post_title == "A"
        assert link.subreddit == "pics"
        assert link.sent_at is not None
        assert link.run_id == run_id
        run = session.query(scanner.Run).filter(scanner.Run.id == run_id).one()
        assert (run.status, run.links_found, run.links_new, run.links_sent) == ("ok", 1, 1, 1)
    finally:
        session.close()
    pushover.assert_called_once_with(
        "1 new media links have been sent to JDownloader!", config)


def test_no_new_links_ping_verbatim(fresh_db, config, monkeypatch):
    posts = saved_posts_json([_post("https://i.imgur.com/a.gif")])
    _mock_net(monkeypatch, posts, 1)
    pushover = Mock()
    monkeypatch.setattr(scanner, "send_pushover_notification", pushover)
    scanner.run_scan(config)  # first run sends it

    pushover.reset_mock()
    scanner.run_scan(config)  # second run: already sent -> no new
    pushover.assert_called_once_with("No new media links found.", config)


def test_no_media_links_ping_verbatim(fresh_db, config, monkeypatch):
    posts = saved_posts_json([_post("https://example.com/article")])
    _mock_net(monkeypatch, posts, 0)
    pushover = Mock()
    monkeypatch.setattr(scanner, "send_pushover_notification", pushover)
    scanner.run_scan(config)
    pushover.assert_called_once_with(
        "No media links found in saved posts.", config)


def test_fetch_failure_ping_verbatim(fresh_db, config, monkeypatch):
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: "tok")
    monkeypatch.setattr(scanner, "fetch_saved_posts", lambda cfg, tok, limit=None: None)
    pushover = Mock()
    monkeypatch.setattr(scanner, "send_pushover_notification", pushover)
    scanner.run_scan(config)
    pushover.assert_called_once_with("Failed to fetch saved posts.", config)


# --- send failure must not burn links ----------------------------------------
def test_send_failure_keeps_links_retryable(fresh_db, config, monkeypatch):
    posts = saved_posts_json([_post("https://i.imgur.com/a.gif")])
    _mock_net(monkeypatch, posts, 0)  # JDownloader refuses everything
    pushover = Mock()
    monkeypatch.setattr(scanner, "send_pushover_notification", pushover)

    scanner.run_scan(config)
    pushover.assert_called_once_with(
        "Failed to send media links to JDownloader.", config)

    session = fresh_db.get_session()
    try:
        link = session.query(Link).one()
        assert link.status == "failed"
        assert link.sent_at is None
        # failed links stay eligible for the next run
        assert scanner.filter_new_links(
            [{"url": "https://i.imgur.com/a.gif"}]) != []
    finally:
        session.close()

    # next run with a working JDownloader picks the same link up and sends it
    _mock_net(monkeypatch, posts, 1)
    pushover.reset_mock()
    scanner.run_scan(config)
    session = fresh_db.get_session()
    try:
        link = session.query(Link).one()
        assert link.status == "sent"
    finally:
        session.close()
    pushover.assert_called_once_with(
        "1 new media links have been sent to JDownloader!", config)


# --- auth watchdog ------------------------------------------------------------
def test_watchdog_alerts_every_third_failure_verbatim(fresh_db):
    pushover = Mock()
    for i in range(1, 7):
        scanner.record_auth_failure(pushover)
        assert scanner.get_auth_failure_count() == i
    assert pushover.call_count == 2  # failures 3 and 6 only
    for call in pushover.call_args_list:
        msg = call.args[0]
        assert msg.startswith("RedSaves: Reddit authentication has failed ")
        assert msg.endswith(
            "runs in a row. Your refresh token may have expired - check your .env file.")
    assert "3 runs in a row" in pushover.call_args_list[0].args[0]
    assert "6 runs in a row" in pushover.call_args_list[1].args[0]


def test_watchdog_resets_on_success(fresh_db):
    pushover = Mock()
    scanner.record_auth_failure(pushover)
    scanner.record_auth_failure(pushover)
    assert scanner.get_auth_failure_count() == 2
    scanner.reset_auth_failures()
    assert scanner.get_auth_failure_count() == 0
    assert pushover.call_count == 0  # never hit the 3rd failure


def test_auth_failure_scan_records_run(fresh_db, config, monkeypatch):
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: None)
    pushover = Mock()
    monkeypatch.setattr(scanner, "send_pushover_notification", pushover)
    run_id = scanner.run_scan(config)
    session = fresh_db.get_session()
    try:
        run = session.query(scanner.Run).filter(scanner.Run.id == run_id).one()
        assert run.status == "auth_failed"
    finally:
        session.close()
    assert scanner.get_auth_failure_count() == 1
    pushover.assert_not_called()  # only every 3rd failure alerts
