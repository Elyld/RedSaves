"""RedSaves WebUI: dashboard, history, runs, settings + scan trigger."""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import desc, func, or_

from app import pages, scheduler
from app import scanner as scan_mod
from app import stats
from app.database import DATA_DIR, get_session, init_db
from app.models import Link, Playlist, PlaylistItem, Run
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
            "enrich_note": scan_mod.get_enrich_note(),
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
                                  imported=imported, skipped=skipped,
                                  missing=scan_mod.titleless_count(),
                                  enrich=scan_mod.get_enrich_progress())
    finally:
        session.close()


@app.post("/history/enrich")
def history_enrich():
    """Start metadata enrichment in a background thread. 409 while one is
    already running."""
    if scan_mod._enrich_lock.locked():
        return JSONResponse({"ok": False, "reason": "already running"}, status_code=409)
    thread = threading.Thread(target=scan_mod.run_enrich, daemon=True)
    thread.start()
    return RedirectResponse("/history", status_code=303)


@app.get("/api/enrich-status")
def api_enrich_status():
    """Progress of the running (or last) enrichment for the History page."""
    return scan_mod.get_enrich_progress()


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


@app.get("/media", response_class=HTMLResponse)
def media(q: str = Query(default=""), author: str = Query(default=""),
          subreddit: str = Query(default=""), creator: str = Query(default=""),
          fav: int = Query(default=0), hide_dead: int = Query(default=0),
          sort: str = Query(default="new")):
    """Media center: filterable video grid with lightbox player."""
    session = get_session()
    try:
        query = session.query(Link)
        if q:
            like = f"%{q}%"
            query = query.filter(or_(
                Link.url.like(like), Link.post_title.like(like),
                Link.subreddit.like(like), Link.author.like(like),
                Link.creator.like(like)))
        if author:
            query = query.filter(Link.author == author)
        if subreddit:
            query = query.filter(Link.subreddit == subreddit)
        if creator:
            query = query.filter(Link.creator == creator)
        if fav:
            query = query.filter(Link.favorite.is_(True))
        if hide_dead:
            query = query.filter(Link.alive.is_(True))
        query = query.order_by(Link.sent_at if sort == "old" else desc(Link.sent_at))
        links = query.all()

        def ranked(col):
            return [(r[0] or "?", r[1]) for r in
                    session.query(col, func.count(Link.id))
                    .filter(col != "").group_by(col)
                    .order_by(desc(func.count(Link.id))).all()]

        return pages.media_page(
            links, q, author, subreddit, creator, fav, hide_dead, sort,
            ranked(Link.author), ranked(Link.subreddit), ranked(Link.creator),
            enrich=scan_mod.get_enrich_progress(),
            linkcheck=scan_mod.get_linkcheck_progress(),
            filescan=scan_mod.get_filescan_progress(),
            playlists=session.query(Playlist).order_by(Playlist.name).all(),
            dupe_count=len(scan_mod.find_dupe_groups()),
            dead_count=session.query(func.count(Link.id))
            .filter(Link.alive.is_(False)).scalar() or 0)
    finally:
        session.close()


@app.post("/api/favorite/{link_id}")
def toggle_favorite(link_id: int):
    session = get_session()
    try:
        link = session.query(Link).filter(Link.id == link_id).first()
        if not link:
            raise HTTPException(status_code=404, detail="not found")
        link.favorite = not link.favorite
        session.commit()
        return JSONResponse({"favorite": link.favorite})
    finally:
        session.close()


@app.post("/media/refresh")
def media_refresh():
    """Backfill thumbnails/stats for already-titled links via saved-scan."""
    if scan_mod._enrich_lock.locked():
        raise HTTPException(status_code=409, detail="already running")
    thread = threading.Thread(target=scan_mod.run_metadata_refresh, daemon=True)
    thread.start()
    return RedirectResponse(url="/media", status_code=303)


@app.post("/media/check-links")
def media_check_links():
    """Background dead-link check over every stored link."""
    if scan_mod._linkcheck_lock.locked():
        raise HTTPException(status_code=409, detail="already running")
    thread = threading.Thread(target=scan_mod.run_link_check, daemon=True)
    thread.start()
    return RedirectResponse(url="/media", status_code=303)


@app.get("/api/linkcheck-status")
def api_linkcheck_status():
    return scan_mod.get_linkcheck_progress()


@app.get("/media-file/{link_id}")
def media_file(link_id: int):
    """Stream a link's locally downloaded file. Path-traversal safe: the
    stored relative path must resolve inside MEDIA_DIR."""
    root = scan_mod.media_dir().resolve()
    session = get_session()
    try:
        link = session.query(Link).filter(Link.id == link_id).first()
        if not link or not link.local_file:
            raise HTTPException(status_code=404, detail="no local file")
        full = (root / link.local_file).resolve()
        if not str(full).startswith(str(root) + os.sep):
            raise HTTPException(status_code=403, detail="outside media dir")
        if not full.is_file():
            raise HTTPException(status_code=404, detail="file gone")
        return FileResponse(str(full))
    finally:
        session.close()


@app.post("/media/scan-files")
def media_scan_files():
    """Match downloaded video files to links in the background."""
    if scan_mod._filescan_lock.locked():
        raise HTTPException(status_code=409, detail="already running")
    thread = threading.Thread(target=scan_mod.run_filescan, daemon=True)
    thread.start()
    return RedirectResponse(url="/media", status_code=303)


@app.get("/api/filescan-status")
def api_filescan_status():
    return scan_mod.get_filescan_progress()


def _stats_ctx(session) -> dict:
    """Collection stats for the Stats page."""
    total = session.query(func.count(Link.id)).scalar() or 0
    urls = [u for (u,) in session.query(Link.url).all()]

    def is_pic(url: str) -> bool:
        try:
            path = urlsplit(url).path.lower()
        except Exception:
            return False
        return path.endswith(scan_mod.IMAGE_EXTS)

    pics = sum(1 for u in urls if is_pic(u))
    favs = session.query(func.count(Link.id)).filter(Link.favorite.is_(True)).scalar() or 0
    dead = session.query(func.count(Link.id)).filter(Link.alive.is_(False)).scalar() or 0
    local = session.query(func.count(Link.id)).filter(Link.local_file != "").scalar() or 0
    play_secs = session.query(func.sum(Link.duration)).filter(Link.duration.isnot(None)).scalar() or 0
    views = session.query(func.sum(Link.views)).filter(Link.views.isnot(None)).scalar() or 0

    def ranked(col, limit=10):
        return [(r[0] or "?", r[1]) for r in
                session.query(col, func.count(Link.id))
                .filter(col != "", col.isnot(None)).group_by(col)
                .order_by(desc(func.count(Link.id)), col).limit(limit).all()]

    month_rows = (session.query(
        func.strftime("%Y-%m", func.coalesce(Link.sent_at, Link.found_at)),
        func.count(Link.id))
        .group_by(func.strftime("%Y-%m", func.coalesce(Link.sent_at, Link.found_at)))
        .order_by(func.strftime("%Y-%m", func.coalesce(Link.sent_at, Link.found_at)))
        .all())
    monthly = []
    for ym, count in month_rows[-12:]:
        try:
            label = datetime.strptime(ym, "%Y-%m").strftime("%b %Y")
        except (TypeError, ValueError):
            continue
        monthly.append((label, count))

    h, rem = divmod(int(play_secs), 3600)
    playtime = f"{h}h {rem // 60}m" if h else f"{rem // 60}m {rem % 60}s"
    return {
        "total": total, "pics": pics, "clips": total - pics,
        "favs": favs, "dead": dead, "local": local,
        "playtime": playtime, "views": views,
        "top_authors": ranked(Link.author),
        "top_subs": ranked(Link.subreddit),
        "top_creators": ranked(Link.creator),
        "monthly": monthly,
    }


@app.get("/stats", response_class=HTMLResponse)
def stats_view():
    session = get_session()
    try:
        return pages.stats_page(_stats_ctx(session))
    finally:
        session.close()


@app.get("/playlists", response_class=HTMLResponse)
def playlists():
    session = get_session()
    try:
        out = []
        for pl in session.query(Playlist).order_by(Playlist.name).all():
            count = (session.query(func.count(PlaylistItem.id))
                     .filter(PlaylistItem.playlist_id == pl.id).scalar() or 0)
            out.append((pl, count))
        return pages.playlists_page(out)
    finally:
        session.close()


@app.post("/playlists")
def playlist_create(name: str = Form(...)):
    name = (name or "").strip()[:128]
    if not name:
        return RedirectResponse("/playlists", status_code=303)
    session = get_session()
    try:
        session.add(Playlist(name=name, created_at=datetime.now()))
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/playlists", status_code=303)


@app.post("/playlists/{playlist_id}/delete")
def playlist_delete(playlist_id: int):
    session = get_session()
    try:
        session.query(PlaylistItem).filter(
            PlaylistItem.playlist_id == playlist_id).delete(synchronize_session=False)
        session.query(Playlist).filter(Playlist.id == playlist_id).delete(
            synchronize_session=False)
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/playlists", status_code=303)


@app.get("/playlists/{playlist_id}", response_class=HTMLResponse)
def playlist_detail(playlist_id: int):
    session = get_session()
    try:
        pl = session.query(Playlist).filter(Playlist.id == playlist_id).first()
        if not pl:
            raise HTTPException(status_code=404, detail="playlist not found")
        links = (session.query(Link)
                 .join(PlaylistItem, PlaylistItem.link_id == Link.id)
                 .filter(PlaylistItem.playlist_id == playlist_id)
                 .order_by(PlaylistItem.added_at).all())
        return pages.playlist_detail_page(pl, links)
    finally:
        session.close()


@app.post("/api/playlists/{playlist_id}/add/{link_id}")
def playlist_add(playlist_id: int, link_id: int):
    session = get_session()
    try:
        pl = session.query(Playlist).filter(Playlist.id == playlist_id).first()
        link = session.query(Link).filter(Link.id == link_id).first()
        if not pl or not link:
            raise HTTPException(status_code=404, detail="not found")
        exists = (session.query(PlaylistItem)
                  .filter(PlaylistItem.playlist_id == playlist_id,
                          PlaylistItem.link_id == link_id).first())
        if not exists:
            session.add(PlaylistItem(playlist_id=playlist_id, link_id=link_id,
                                    added_at=datetime.now()))
            session.commit()
        return JSONResponse({"ok": True})
    finally:
        session.close()


@app.post("/api/playlists/{playlist_id}/remove/{link_id}")
def playlist_remove(playlist_id: int, link_id: int):
    session = get_session()
    try:
        session.query(PlaylistItem).filter(
            PlaylistItem.playlist_id == playlist_id,
            PlaylistItem.link_id == link_id).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()
    return RedirectResponse(f"/playlists/{playlist_id}", status_code=303)


@app.get("/dupes", response_class=HTMLResponse)
def dupes():
    return pages.dupes_page(scan_mod.find_dupe_groups())


@app.post("/dupes/merge")
def dupes_merge(winner_id: int = Form(...), loser_ids: str = Form(default="")):
    losers = [int(x) for x in loser_ids.split(",") if x.strip().isdigit()]
    scan_mod.merge_dupe_group(winner_id, losers)
    return RedirectResponse("/dupes", status_code=303)


@app.post("/dupes/merge-all")
def dupes_merge_all():
    for g in scan_mod.find_dupe_groups():
        scan_mod.merge_dupe_group(g["winner_id"],
                                  [l.id for l in g["links"]])
    return RedirectResponse("/dupes", status_code=303)


@app.get("/graveyard", response_class=HTMLResponse)
def graveyard():
    session = get_session()
    try:
        links = (session.query(Link).filter(Link.alive.is_(False))
                 .order_by(desc(Link.checked_at)).all())
        return pages.graveyard_page(links)
    finally:
        session.close()


@app.post("/graveyard/purge")
def graveyard_purge():
    session = get_session()
    try:
        (session.query(Link).filter(Link.alive.is_(False))
         .delete(synchronize_session=False))
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/graveyard", status_code=303)


@app.post("/graveyard/delete/{link_id}")
def graveyard_delete(link_id: int):
    session = get_session()
    try:
        session.query(Link).filter(Link.id == link_id).delete(
            synchronize_session=False)
        session.commit()
    finally:
        session.close()
    return RedirectResponse("/graveyard", status_code=303)


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
