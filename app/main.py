"""RedSaves WebUI: dashboard, history, runs, settings + scan trigger."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_

from app import pages, scheduler
from app import scanner as scan_mod
from app import stats
from app.database import DATA_DIR, get_session, init_db
from app.models import Link, Run
from app.version import APP_NAME, VERSION


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": VERSION}


def _dashboard_ctx() -> dict:
    session = get_session()
    try:
        last = session.query(Run).order_by(desc(Run.id)).first()
        total_sent = session.query(func.count(Link.id)).filter(Link.status == "sent").scalar() or 0
        week_ago = datetime.now() - timedelta(days=7)
        sent_7d = session.query(func.count(Link.id)).filter(
            Link.status == "sent", Link.sent_at >= week_ago).scalar() or 0
        return {
            "last_run": last,
            "total_sent": total_sent,
            "sent_7d": sent_7d,
            "watchdog": scan_mod.get_auth_failure_count(),
            "scan_running": scan_mod._scan_lock.locked(),
            "next_runs": scheduler.next_run_times(),
            "upvoted_warning": scan_mod.upvoted_scope_missing(),
        }
    finally:
        session.close()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return pages.dashboard_page(_dashboard_ctx())


PAGE_SIZE = 50


@app.get("/history", response_class=HTMLResponse)
def history(q: str = Query(default=""), page: int = Query(default=1, ge=1),
            imported: int = Query(default=None), skipped: int = Query(default=None)):
    session = get_session()
    try:
        query = session.query(Link)
        if q:
            like = f"%{q}%"
            query = query.filter(or_(
                Link.url.like(like), Link.post_title.like(like),
                Link.subreddit.like(like), Link.author.like(like),
                Link.source.like(like)))
        total = query.count()
        pages_count = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages_count)
        links = (query.order_by(desc(Link.id))
                 .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all())
        return pages.history_page(links, q, page, pages_count,
                                  imported=imported, skipped=skipped)
    finally:
        session.close()


@app.post("/history/import")
async def history_import(file: UploadFile = File(...)):
    """Import a sent_links.txt-format file (one URL per line). Dedupes against
    existing URLs; inserts the rest as already-sent. Works whether or not the
    table already has rows (unlike the boot migration's empty-table guard)."""
    raw = await file.read()
    urls = [ln.strip() for ln in raw.decode("utf-8", errors="replace").splitlines()
            if ln.strip()]
    session = get_session()
    try:
        existing = {u for (u,) in session.query(Link.url).all()}
        imported = skipped = 0
        now = datetime.now()
        for url in dict.fromkeys(urls):  # dedupe within the file, keep order
            if url in existing:
                skipped += 1
            else:
                session.add(Link(url=url, status="sent", sent_at=now, found_at=now))
                existing.add(url)
                imported += 1
        session.commit()
    finally:
        session.close()
    return RedirectResponse(
        f"/history?imported={imported}&skipped={skipped}", status_code=303)


@app.get("/top", response_class=HTMLResponse)
def top():
    session = get_session()
    try:
        return pages.top_page(
            stats.top_subreddits(session),
            stats.top_authors(session),
            stats.links_per_week(session),
        )
    finally:
        session.close()


@app.get("/logs", response_class=HTMLResponse)
def logs():
    return pages.logs_view()


@app.get("/api/logs")
def api_logs():
    """Tail of the run log for the Logs page (last 300 lines)."""
    log_file = DATA_DIR / "redsaves.log"
    try:
        lines = log_file.read_text(errors="replace").splitlines()[-300:]
    except OSError:
        lines = []
    return {"lines": lines}


@app.get("/runs", response_class=HTMLResponse)
def runs():
    session = get_session()
    try:
        rows = session.query(Run).order_by(desc(Run.id)).limit(100).all()
        return pages.runs_page(rows)
    finally:
        session.close()


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(run_id: int):
    session = get_session()
    try:
        run = session.query(Run).filter(Run.id == run_id).first()
        if not run:
            raise HTTPException(404, "run not found")
        links = session.query(Link).filter(Link.run_id == run_id).all()
        return pages.run_detail_page(run, links)
    finally:
        session.close()


def _settings_dict() -> dict:
    return {
        "scan_time_1": scan_mod.get_setting_value("scan_time_1", "08:00"),
        "scan_time_2": scan_mod.get_setting_value("scan_time_2", "20:00"),
        "notify_results": scan_mod.get_setting_value("notify_results", "1"),
        "notify_watchdog": scan_mod.get_setting_value("notify_watchdog", "1"),
        "post_limit": str(scan_mod.get_post_limit()),
        "include_upvoted": scan_mod.get_setting_value("include_upvoted", "0"),
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_get(request: Request):
    saved = request.query_params.get("saved") == "1"
    return pages.settings_page(_settings_dict(), saved)


@app.post("/settings")
def settings_post(
    scan_time_1: str = Form(...),
    scan_time_2: str = Form(...),
    notify_results: str = Form(default="0"),
    notify_watchdog: str = Form(default="0"),
    post_limit: str = Form(default="25"),
    include_upvoted: str = Form(default="0"),
):
    scan_mod.set_setting_value("scan_time_1", scan_time_1)
    scan_mod.set_setting_value("scan_time_2", scan_time_2)
    scan_mod.set_setting_value("notify_results", "1" if notify_results == "1" else "0")
    scan_mod.set_setting_value("notify_watchdog", "1" if notify_watchdog == "1" else "0")
    scan_mod.set_post_limit(post_limit)  # validates + clamps 1..100 server-side
    scan_mod.set_setting_value("include_upvoted", "1" if include_upvoted == "1" else "0")
    scheduler.schedule_from_settings()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/api/dismiss-upvoted-warning")
def dismiss_upvoted_warning():
    """Clear the upvoted-scope dashboard warning. It comes back on its own if
    a later scan's upvoted fetch still fails on auth scope."""
    scan_mod.set_upvoted_scope_missing(False)
    return RedirectResponse("/", status_code=303)


@app.post("/api/scan")
def trigger_scan():
    """Start a scan in a background thread. 409 while one is already running."""
    if scan_mod._scan_lock.locked():
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)
    thread = threading.Thread(target=scan_mod.run_scan, daemon=True)
    thread.start()
    return RedirectResponse("/", status_code=303)


@app.get("/api/status")
def api_status():
    ctx = _dashboard_ctx()
    last = ctx["last_run"]
    return {
        "scan_running": ctx["scan_running"],
        "watchdog_failures": ctx["watchdog"],
        "total_sent": ctx["total_sent"],
        "sent_7d": ctx["sent_7d"],
        "next_runs": ctx["next_runs"],
        "last_run": (
            {"id": last.id, "status": last.status,
             "started_at": last.started_at.isoformat() if last.started_at else None,
             "finished_at": last.finished_at.isoformat() if last.finished_at else None,
             "links_found": last.links_found, "links_new": last.links_new,
             "links_sent": last.links_sent, "error": last.error}
            if last else None
        ),
    }
