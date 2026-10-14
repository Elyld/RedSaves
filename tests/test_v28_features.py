"""v2.8.0: "Enrich missing info" — backfill title/author/subreddit via /api/info."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest

import app.database as db
import app.scanner as scanner
from app.main import app
from app.models import Link


def _add_link(session, url, title="", author="", subreddit="", permalink=""):
    link = Link(url=url, post_title=title, post_permalink=permalink,
                subreddit=subreddit, author=author, status="sent",
                sent_at=datetime.now(), found_at=datetime.now())
    session.add(link)
    session.commit()
    return link.id  # id, not the instance — the session gets closed by callers


def _info_payload(title="Found title", permalink="/r/pics/comments/abc",
                  subreddit="pics", author="someposter"):
    return {"data": {"children": [{"data": {
        "title": title, "permalink": permalink,
        "subreddit": subreddit, "author": author}}]}}


def _fake_info(monkeypatch, by_url):
    """Fake requests.get for /api/info. by_url maps media URL -> payload or
    (status_code, payload). Returns the list of requested media URLs."""
    requested = []

    def fake_get(url, params=None, headers=None, timeout=None):
        assert url.endswith("/api/info")
        media_url = (params or {}).get("url")
        requested.append(media_url)
        resp = Mock()
        entry = by_url.get(media_url, ({"data": {"children": []}}))
        if isinstance(entry, tuple):
            resp.status_code, payload = entry
        else:
            resp.status_code, payload = 200, entry
        resp.json.return_value = payload
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    return requested


def _no_throttle(monkeypatch, counter=None):
    calls = counter if counter is not None else []
    monkeypatch.setattr(scanner, "_throttle", lambda: calls.append(1))
    return calls


@pytest.fixture()
def token_ok(monkeypatch):
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: "tok")


# --- core behavior -----------------------------------------------------------
def test_enrich_fills_all_fields(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    link_id = _add_link(session, "https://i.imgur.com/a.gif")
    session.close()
    _fake_info(monkeypatch, {"https://i.imgur.com/a.gif": _info_payload()})
    _no_throttle(monkeypatch)

    assert scanner.run_enrich(config) is True

    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == "Found title"
        assert row.post_permalink == "/r/pics/comments/abc"
        assert row.subreddit == "pics"
        assert row.author == "someposter"
        assert row.source == "saved"  # untouched
    finally:
        session.close()


def test_enrich_empty_results_leaves_row_alone(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    link_id = _add_link(session, "https://i.imgur.com/b.gif")
    session.close()
    _fake_info(monkeypatch, {"https://i.imgur.com/b.gif": {"data": {"children": []}}})
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == ""
        assert row.author == ""
    finally:
        session.close()
    assert scanner.get_enrich_progress()["skipped"] == 1
    assert scanner.get_enrich_progress()["found"] == 0


def test_enrich_http_error_leaves_row_alone(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    link_id = _add_link(session, "https://i.imgur.com/c.gif")
    session.close()
    _fake_info(monkeypatch, {"https://i.imgur.com/c.gif": (500, {})})
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    session = db.get_session()
    try:
        assert session.query(Link).filter(Link.id == link_id).first().post_title == ""
    finally:
        session.close()
    assert scanner.get_enrich_progress()["skipped"] == 1


def test_enrich_only_titleless_links_processed(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    _add_link(session, "https://i.imgur.com/titled.gif", title="Already here")
    _add_link(session, "https://i.imgur.com/untitled.gif")
    session.close()
    requested = _fake_info(monkeypatch, {
        "https://i.imgur.com/untitled.gif": _info_payload(title="New title"),
    })
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    assert requested == ["https://i.imgur.com/untitled.gif"]
    session = db.get_session()
    try:
        titled = session.query(Link).filter(
            Link.url == "https://i.imgur.com/titled.gif").first()
        assert titled.post_title == "Already here"  # never overwritten
    finally:
        session.close()


def test_enrich_never_overwrites_existing_fields(fresh_db, monkeypatch, config, token_ok):
    # Titleless (so it IS processed), but author was set manually — the
    # enrichment must fill the title without clobbering the existing author.
    session = db.get_session()
    link_id = _add_link(session, "https://i.imgur.com/d.gif", author="manualauthor")
    session.close()
    _fake_info(monkeypatch, {"https://i.imgur.com/d.gif": _info_payload(
        title="Other title", author="newauthor")})
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == "Other title"   # empty field filled
        assert row.author == "manualauthor"      # existing value kept
        assert row.subreddit == "pics"
    finally:
        session.close()


def test_enrich_throttles_every_request(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    _add_link(session, "https://i.imgur.com/e1.gif")
    _add_link(session, "https://i.imgur.com/e2.gif")
    _add_link(session, "https://i.imgur.com/e3.gif", title="skip me")
    session.close()
    requested = _fake_info(monkeypatch, {})
    calls = _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    # one throttle per actual /api/info request (variants included), so we
    # stay under Reddit's rate limit even when every variant is tried
    assert len(calls) == len(requested) > 2


def test_enrich_progress_counts(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    _add_link(session, "https://i.imgur.com/f1.gif")
    _add_link(session, "https://i.imgur.com/f2.gif")
    _add_link(session, "https://i.imgur.com/f3.gif")
    session.close()
    _fake_info(monkeypatch, {
        "https://i.imgur.com/f1.gif": _info_payload(),
        "https://i.imgur.com/f2.gif": _info_payload(),
        # f3 -> empty children -> skipped
    })
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    p = scanner.get_enrich_progress()
    # phase 1 found 2 of 3; phase 2 (saved-scan) found nothing for the last one
    assert p == {"running": False, "phase": "saved-scan", "processed": 0,
                "total": 1, "found": 2, "skipped": 1}


def test_enrich_concurrent_second_run_refused(fresh_db, config):
    scanner._enrich_lock.acquire()
    try:
        assert scanner.run_enrich(config) is False
    finally:
        scanner._enrich_lock.release()
    # and the lock is usable again afterwards
    assert scanner._enrich_lock.acquire(blocking=False)
    scanner._enrich_lock.release()


def test_enrich_no_token_aborts_with_note(fresh_db, monkeypatch, config):
    monkeypatch.setattr(scanner, "get_access_token", lambda cfg: None)
    calls = []

    def fake_get(*a, **k):
        calls.append(1)
        raise AssertionError("should not request without a token")

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    session = db.get_session()
    _add_link(session, "https://i.imgur.com/g.gif")
    session.close()

    scanner._run_enrich(config)

    assert calls == []
    assert scanner.get_enrich_progress()["running"] is False
    note = scanner.get_enrich_note()
    assert "token" in note.lower()


def test_titleless_count(fresh_db):
    session = db.get_session()
    _add_link(session, "https://i.imgur.com/h1.gif")
    _add_link(session, "https://i.imgur.com/h2.gif", title="Has one")
    session.close()
    assert scanner.titleless_count() == 1


# --- routes ------------------------------------------------------------------
def _seed_titled_and_untitled(fresh_db):
    session = db.get_session()
    try:
        _add_link(session, "https://i.imgur.com/i1.gif")
        _add_link(session, "https://i.imgur.com/i2.gif")
        _add_link(session, "https://i.imgur.com/i3.gif", title="Titled")
    finally:
        session.close()


def test_history_shows_enrich_button_with_count(fresh_db):
    _seed_titled_and_untitled(fresh_db)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        html = client.get("/history").text
        assert "Enrich missing info (2)" in html
        assert 'action="/history/enrich"' in html


def test_history_hides_button_when_none_missing(fresh_db):
    session = db.get_session()
    try:
        _add_link(session, "https://i.imgur.com/j1.gif", title="All good")
    finally:
        session.close()
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        html = client.get("/history").text
        assert "Enrich missing info" not in html


def test_enrich_status_endpoint(fresh_db):
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get("/api/enrich-status")
        assert r.status_code == 200
        assert set(r.json()) == {"running", "phase", "processed", "total", "found", "skipped"}


def test_enrich_route_409_when_running(fresh_db, monkeypatch):
    scanner._enrich_lock.acquire()
    try:
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            r = client.post("/history/enrich")
            assert r.status_code == 409
    finally:
        scanner._enrich_lock.release()


def test_enrich_backfills_date_from_post_created_utc(fresh_db, monkeypatch, config, token_ok):
    """Imported bare URLs get sent_at from the post's creation date instead of
    the meaningless import date; links that already had titles keep theirs."""
    from datetime import datetime as dt
    session = db.get_session()
    bare_id = _add_link(session, "https://i.imgur.com/c.gif")  # no title
    titled_id = _add_link(session, "https://i.imgur.com/d.gif", title="Already titled")
    session.close()

    created = 1700000000  # 2023-11-14
    payload = {"data": {"children": [{"data": {
        "title": "Old post", "permalink": "/r/pics/comments/xyz",
        "subreddit": "pics", "author": "oldposter", "created_utc": created}}]}}
    _fake_info(monkeypatch, {"https://i.imgur.com/c.gif": payload,
                             "https://i.imgur.com/d.gif": payload})
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    session = db.get_session()
    try:
        bare = session.query(Link).filter(Link.id == bare_id).first()
        titled = session.query(Link).filter(Link.id == titled_id).first()
        assert bare.sent_at == dt.fromtimestamp(created)
        # titled link: run_enrich only processes title-less links, untouched
        assert titled.post_title == "Already titled"
    finally:
        session.close()


def test_enrich_tries_url_variants(fresh_db, monkeypatch, config, token_ok):
    """A www-less URL in the file matches via the www variant."""
    session = db.get_session()
    link_id = _add_link(session, "https://redgifs.com/watch/abc123")
    session.close()
    payload = {"data": {"children": [{"data": {
        "title": "Variant hit", "permalink": "/r/x/comments/1",
        "subreddit": "x", "author": "u1", "created_utc": 1700000000}}]}}
    empty = {"data": {"children": []}}
    requested = _fake_info(monkeypatch, {
        "https://redgifs.com/watch/abc123": empty,
        "https://www.redgifs.com/watch/abc123": payload,
    })
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    assert "https://www.redgifs.com/watch/abc123" in requested
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == "Variant hit"
        assert row.author == "u1"
    finally:
        session.close()


def test_url_variants_dedupes_and_caps():
    v = scanner._url_variants("https://www.example.com/a.gif")
    assert v[0] == "https://example.com/a.gif"
    assert "https://www.example.com/a.gif" in v
    assert len(v) <= 6
    assert len(set(v)) == len(v)
    v2 = scanner._url_variants("http://example.com/b.mp4")
    assert "https://example.com/b.mp4" in v2
    assert "https://www.example.com/b.mp4" in v2


def test_canonicalize_vredd_it_strips_media_path():
    assert (scanner._canonicalize_url("https://v.redd.it/wlfxk26lsvah1/CMAF_1080.mp4")
            == "https://v.redd.it/wlfxk26lsvah1")
    v = scanner._url_variants("https://v.redd.it/wlfxk26lsvah1/CMAF_1080.mp4")
    assert v[0] == "https://v.redd.it/wlfxk26lsvah1"


def test_canonicalize_redgifs_forms():
    assert (scanner._canonicalize_url("https://v3.redgifs.com/watch/AbcXyz")
            == "https://www.redgifs.com/watch/AbcXyz")
    assert (scanner._canonicalize_url("https://Www.redgifs.com/watch/AbcXyz")
            == "https://www.redgifs.com/watch/AbcXyz")
    assert (scanner._canonicalize_url("https://www.redgifs.com/ifr/AbcXyz")
            == "https://www.redgifs.com/watch/AbcXyz")
    # already-canonical stays first
    v = scanner._url_variants("https://www.redgifs.com/watch/AbcXyz")
    assert v[0] == "https://www.redgifs.com/watch/AbcXyz"


# --- phase 2: saved-posts deep match -----------------------------------------

def _fake_reddit(monkeypatch, info_by_url, saved_pages):
    """Fake requests.get routing /api/info and /saved. saved_pages is a list
    of (children, after) tuples; children are raw post data dicts."""
    requested = []

    def fake_get(url, params=None, headers=None, timeout=None):
        requested.append((url, dict(params or {})))
        resp = Mock()
        resp.status_code = 200
        if url.endswith("/api/info"):
            media_url = (params or {}).get("url")
            resp.json.return_value = info_by_url.get(
                media_url, {"data": {"children": []}})
        elif url.endswith("/saved"):
            idx = len([r for r in requested if r[0].endswith("/saved")]) - 1
            children, after = saved_pages[min(idx, len(saved_pages) - 1)]
            resp.json.return_value = {"data": {
                "children": [{"data": c} for c in children], "after": after}}
        else:
            raise AssertionError(f"unexpected url {url}")
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    return requested


def _saved_post(url, title="Saved title", subreddit="pics",
                author="saveposter", permalink="/r/pics/comments/s1",
                created=1700000000):
    return {"url": url, "title": title, "subreddit": subreddit,
            "author": author, "permalink": permalink, "created_utc": created}


def test_deep_match_fills_from_saved_posts(fresh_db, monkeypatch, config, token_ok):
    """A v.redd.it media-file URL in the DB matches the bare-ID saved post."""
    session = db.get_session()
    link_id = _add_link(session, "https://v.redd.it/abc123/CMAF_1080.mp4")
    session.close()
    _fake_reddit(monkeypatch, {}, [
        ([_saved_post("https://v.redd.it/abc123")], None),
    ])
    _no_throttle(monkeypatch)

    assert scanner.run_enrich(config) is True

    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == "Saved title"
        assert row.author == "saveposter"
        assert row.subreddit == "pics"
        assert row.post_permalink == "/r/pics/comments/s1"
        from datetime import datetime as dt
        assert row.sent_at == dt.fromtimestamp(1700000000)
    finally:
        session.close()


def test_deep_match_pages_through_saved(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    id1 = _add_link(session, "https://v.redd.it/aaa111/DASH_720.mp4")
    id2 = _add_link(session, "https://www.redgifs.com/watch/someslug")
    session.close()
    _fake_reddit(monkeypatch, {}, [
        ([_saved_post("https://v.redd.it/aaa111", title="First")], "t3_after1"),
        ([_saved_post("https://v3.redgifs.com/watch/someslug", title="Second")], None),
    ])
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    session = db.get_session()
    try:
        r1 = session.query(Link).filter(Link.id == id1).first()
        r2 = session.query(Link).filter(Link.id == id2).first()
        assert r1.post_title == "First"
        assert r2.post_title == "Second"  # v3 form matched www form
    finally:
        session.close()


def test_deep_match_ignores_nonmedia_and_adds_nothing(fresh_db, monkeypatch, config, token_ok):
    session = db.get_session()
    link_id = _add_link(session, "https://v.redd.it/zzz999/CMAF_1080.mp4")
    before = session.query(Link).count()
    session.close()
    _fake_reddit(monkeypatch, {}, [
        ([_saved_post("https://example.com/article", title="Not media"),
          _saved_post("https://v.redd.it/other000", title="Unrelated media")], None),
    ])
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    session = db.get_session()
    try:
        assert session.query(Link).count() == before  # nothing added
        row = session.query(Link).filter(Link.id == link_id).first()
        assert row.post_title == ""  # unmatched, untouched
    finally:
        session.close()
    assert scanner.get_enrich_progress()["skipped"] == 1


def test_enrich_aborts_lookup_phase_on_repeated_403(fresh_db, monkeypatch, config, token_ok):
    """Systematic 403s abort phase 1 after 3 links; the saved-scan still runs."""
    session = db.get_session()
    ids = [_add_link(session, f"https://v.redd.it/u{i:06d}/CMAF_1080.mp4") for i in range(5)]
    session.close()

    info_calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        resp = Mock()
        if url.endswith("/api/info"):
            info_calls.append((params or {}).get("url"))
            resp.status_code = 403
            resp.json.return_value = {}
        elif url.endswith("/saved"):
            resp.status_code = 200
            # saved scan matches the 4th link via its bare-ID post
            resp.json.return_value = {"data": {
                "children": [{"data": _saved_post("https://v.redd.it/u000003")}],
                "after": None}}
        else:
            raise AssertionError(url)
        return resp

    monkeypatch.setattr(scanner.requests, "get", fake_get)
    _no_throttle(monkeypatch)

    scanner.run_enrich(config)

    # phase 1 stopped after 3 refused links instead of burning 5 links x variants
    assert len(info_calls) == 3
    session = db.get_session()
    try:
        row = session.query(Link).filter(Link.id == ids[3]).first()
        assert row.post_title == "Saved title"  # phase 2 still did its job
    finally:
        session.close()
    assert "403" in scanner.get_enrich_note()
