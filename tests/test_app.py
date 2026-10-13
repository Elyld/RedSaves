"""Migration + web smoke tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import app.database as db
import app.scanner as scanner
from app.main import app
from app.models import Link, Run


def test_migrate_legacy_history(fresh_db, tmp_path):
    (tmp_path / "sent_links.txt").write_text(
        "https://i.imgur.com/a.gif\nhttps://v.redd.it/xyz\n\n")
    # make sure the repo-root candidate doesn't interfere
    migrated = db.migrate_legacy_history()
    assert migrated == 2
    session = db.get_session()
    try:
        rows = session.query(Link).all()
        assert len(rows) == 2
        assert all(r.status == "sent" and r.sent_at is not None for r in rows)
    finally:
        session.close()
    # second call is a no-op (already migrated)
    assert db.migrate_legacy_history() == 0


def test_migrate_skips_duplicates(fresh_db, tmp_path):
    (tmp_path / "sent_links.txt").write_text("https://i.imgur.com/a.gif\n")
    assert db.migrate_legacy_history() == 1
    session = db.get_session()
    try:
        session.add(Link(url="https://i.imgur.com/b.gif", status="sent"))
        session.commit()
    finally:
        session.close()
    # links table non-empty now -> migration refuses to run again
    assert db.migrate_legacy_history() == 0


def _seed(fresh_db):
    session = db.get_session()
    try:
        run = Run(started_at=datetime.now() - timedelta(hours=2),
                  finished_at=datetime.now() - timedelta(hours=2),
                  status="ok", links_found=3, links_new=2, links_sent=2)
        session.add(run)
        session.commit()
        session.add(Link(url="https://i.imgur.com/a.gif", post_title="Cool gif",
                         post_permalink="/r/pics/comments/1", subreddit="pics",
                         status="sent", sent_at=datetime.now(), run_id=run.id))
        session.add(Link(url="https://v.redd.it/xyz", post_title="Vid",
                         subreddit="videos", status="failed",
                         found_at=datetime.now(), run_id=run.id))
        session.commit()
    finally:
        session.close()


def test_health_endpoint(fresh_db):
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_dashboard_renders(fresh_db):
    _seed(fresh_db)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        html = r.text
        assert "Links sent (all time)" in html
        assert "Scan now" in html


def test_history_and_runs_render(fresh_db):
    _seed(fresh_db)
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        assert client.get("/history").status_code == 200
        assert "Cool gif" in client.get("/history").text
        assert client.get("/history?q=pics").status_code == 200
        runs = client.get("/runs")
        assert runs.status_code == 200
        detail = client.get("/runs/1")
        assert detail.status_code == 200
        assert "Run #1" in detail.text
        assert client.get("/runs/999").status_code == 404


def test_settings_roundtrip(fresh_db):
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        assert client.get("/settings").status_code == 200
        r = client.post("/settings", data={
            "scan_time_1": "07:30", "scan_time_2": "19:30",
            "notify_results": "1",
            # notify_watchdog checkbox omitted -> off
        })
        assert r.status_code in (200, 303)
        assert scanner.get_setting_value("scan_time_1") == "07:30"
        assert scanner.get_setting_value("notify_watchdog") == "0"
