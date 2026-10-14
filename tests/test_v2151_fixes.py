"""v2.15.1: stress-test hardening fixes."""

from __future__ import annotations

from datetime import datetime

import app.database as db
import app.pages as pages
import app.scanner as scanner
from app.main import app
from app.models import Link, Run


def _add_link(session, url, title="t", alive=True):
    link = Link(url=url, post_title=title, post_permalink="", status="sent",
                sent_at=datetime.now(), found_at=datetime.now(), alive=alive,
                checked_at=datetime.now())
    session.add(link)
    session.commit()
    return link


def test_media_json_escapes_script_breakout(fresh_db):
    """A </script> in a Reddit title must not break out of the data blob."""
    session = db.get_session()
    link = _add_link(session, "https://v.redd.it/a1/x.mp4",
                     title='</script><script>alert(1)</script>')
    html = pages.media_page([link])
    session.close()
    assert "</script><script>" not in html
    assert "\\u003c/script>" in html


def test_wal_mode_enabled(fresh_db):
    from sqlalchemy import text
    engine = db.get_engine()
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert mode.lower() == "wal"


def test_graveyard_pagination(fresh_db):
    from fastapi.testclient import TestClient
    session = db.get_session()
    for n in range(120):
        _add_link(session, f"https://v.redd.it/d{n}/x.mp4", title=f"dead {n}",
                  alive=False)
    session.close()
    with TestClient(app) as client:
        r1 = client.get("/graveyard")
        assert r1.status_code == 200
        assert r1.text.count("<tr><td>") == 100
        assert "120 total" in r1.text
        assert "Next »" in r1.text
        r2 = client.get("/graveyard", params={"page": 2})
        assert r2.text.count("<tr><td>") == 20
        assert "« Prev" in r2.text


def test_canonicalize_unifies_www_and_scheme():
    a = scanner._canonicalize_url("https://www.imgur.com/gallery/abc")
    b = scanner._canonicalize_url("https://imgur.com/gallery/abc")
    c = scanner._canonicalize_url("http://imgur.com/gallery/abc")
    assert a == b == c == "https://imgur.com/gallery/abc"
    # redgifs keeps its established www form
    assert (scanner._canonicalize_url("https://v3.redgifs.com/watch/Xyz")
            == "https://www.redgifs.com/watch/Xyz")


def test_dupe_groups_catch_url_variants(fresh_db):
    session = db.get_session()
    _add_link(session, "https://www.imgur.com/gallery/abc", title="one")
    _add_link(session, "http://imgur.com/gallery/abc", title="two")
    session.close()
    groups = scanner.find_dupe_groups()
    assert len(groups) == 1
    assert {l.post_title for l in groups[0]["links"]} == {"one", "two"}


def test_dupe_cache_ttl(fresh_db):
    session = db.get_session()
    _add_link(session, "https://v.redd.it/a1/x.mp4", title="x")
    session.close()
    scanner.invalidate_dupe_cache()
    first = scanner.find_dupe_groups_cached()
    second = scanner.find_dupe_groups_cached()
    assert first is second  # served from cache
    scanner.invalidate_dupe_cache()
    third = scanner.find_dupe_groups_cached()
    assert third is not first  # refreshed after invalidation
    assert third == first


def test_prune_old_runs(fresh_db):
    session = db.get_session()
    for _ in range(205):
        session.add(Run(started_at=datetime.now(), status="ok",
                        finished_at=datetime.now()))
    session.commit()
    pruned = scanner._prune_old_runs(session)
    session.commit()
    remaining = session.query(Run).count()
    session.close()
    assert pruned == 5
    assert remaining == 200
