"""Server-rendered HTML pages for RedSaves."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from html import escape as _esc

from app.scanner import media_embed
from app.version import APP_NAME


def esc(s) -> str:
    return _esc("" if s is None else str(s))


def fmt_dt(dt) -> str:
    if not dt:
        return "—"
    if isinstance(dt, str):
        return dt
    return dt.strftime("%b %-d, %Y %-I:%M %p")


def status_badge(status: str) -> str:
    colors = {
        "ok": "badge-ok",
        "no_new": "badge-idle",
        "auth_failed": "badge-bad",
        "fetch_failed": "badge-bad",
        "send_failed": "badge-warn",
        "running": "badge-run",
        "sent": "badge-ok",
        "failed": "badge-warn",
    }
    label = {
        "ok": "OK", "no_new": "No new", "auth_failed": "Auth failed",
        "fetch_failed": "Fetch failed", "send_failed": "Send failed",
        "running": "Running", "sent": "Sent", "failed": "Failed",
    }.get(status, status)
    return f'<span class="badge {colors.get(status, "")}">{esc(label)}</span>'


def layout(title: str, body: str, active: str = "") -> str:
    def nav(href: str, label: str, key: str) -> str:
        cls = ' class="active"' if active == key else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · {esc(APP_NAME)}</title>
<link rel="stylesheet" href="/static/css/style.css">
</head>
<body>
<header class="topbar">
  <span class="brand">📥 {esc(APP_NAME)}</span>
  <nav>
    {nav("/", "Dashboard", "dash")}
    {nav("/history", "History", "history")}
    {nav("/media", "Media", "media")}
    {nav("/playlists", "Playlists", "playlists")}
    {nav("/stats", "Stats", "stats")}
    {nav("/top", "Top", "top")}
    {nav("/runs", "Runs", "runs")}
    {nav("/logs", "Logs", "logs")}
    {nav("/settings", "Settings", "settings")}
  </nav>
</header>
<main>{body}</main>
<script src="/static/js/app.js"></script>
</body>
</html>"""


def dashboard_page(ctx: dict) -> str:
    last = ctx.get("last_run")
    next_runs = ctx.get("next_runs", [])
    next_line = "<br>".join(esc(t) for t in next_runs) if next_runs else "not scheduled"
    scan_state = "running" if ctx.get("scan_running") else "idle"
    watch = ctx.get("watchdog", 0)
    watch_line = (f'<p class="warn">⚠️ Reddit auth has failed {watch} run(s) in a row.</p>'
                  if watch else '<p class="dim">Reddit auth healthy.</p>')
    last_line = (f"{status_badge(last.status)} &nbsp;{fmt_dt(last.finished_at or last.started_at)}"
                 if last else '<span class="dim">no runs yet</span>')
    upvoted_banner = ""
    if ctx.get("upvoted_warning"):
        upvoted_banner = """
<div class="warnbox">
  <p>⚠️ <b>Upvoted posts aren't being scanned.</b> Your Reddit token lacks the
  <span class="mono">history</span> scope — re-authorize the app to include
  upvoted posts. Saved posts are still scanned normally.</p>
  <form method="post" action="/api/dismiss-upvoted-warning">
    <button type="submit" class="btn">Dismiss</button>
  </form>
</div>"""
    body = f"""
<h1>Dashboard</h1>
{upvoted_banner}
{f'<div class="warnbox"><p>ℹ️ {esc(ctx.get("enrich_note"))}</p></div>' if ctx.get("enrich_note") else ""}
<div class="cards">
  <div class="card"><div class="k">Last run</div><div class="v">{last_line}</div></div>
  <div class="card"><div class="k">Next scheduled</div><div class="v small">{next_line}</div></div>
  <div class="card"><div class="k">Scanner</div><div class="v">{esc(scan_state)}</div></div>
  <div class="card"><div class="k">Links sent (all time)</div><div class="v">{ctx.get("total_sent", 0)}</div></div>
  <div class="card"><div class="k">Links sent (7 days)</div><div class="v">{ctx.get("sent_7d", 0)}</div></div>
  <div class="card"><div class="k">Watchdog</div><div class="v">{watch_line}</div></div>
</div>
<form method="post" action="/api/scan" class="scanbox">
  <button type="submit" class="bigbtn">▶ Scan now</button>
  <span class="dim">Runs in the background; this page refreshes when it finishes.</span>
</form>
"""
    return layout("Dashboard", body, "dash")


def history_page(links, q: str, page: int, pages: int, imported=None, skipped=None,
                 missing: int = 0, enrich: dict | None = None) -> str:
    rows = []
    for link in links:
        title = esc(link.post_title or "(no title)")
        if link.post_permalink:
            title = f'<a href="https://www.reddit.com{esc(link.post_permalink)}" target="_blank" rel="noopener">{title}</a>'
        author = f"u/{esc(link.author)}" if link.author else "—"
        source = esc(link.source or "saved")
        rows.append(
            f"<tr><td>{status_badge(link.status)}</td>"
            f"<td>{title}<br><span class='dim mono'>{esc(link.url[:90])}</span></td>"
            f"<td>{author}</td>"
            f"<td>{esc(link.subreddit or '—')}</td>"
            f"<td class='dim'>{source}</td>"
            f"<td class='nowrap'>{fmt_dt(link.sent_at or link.found_at)}</td></tr>"
        )
    pager = ""
    if pages > 1:
        pager = f'<div class="pager">Page {page} of {pages} &nbsp;'
        if page > 1:
            pager += f'<a href="/history?q={esc(q)}&page={page - 1}">← Prev</a> '
        if page < pages:
            pager += f'<a href="/history?q={esc(q)}&page={page + 1}">Next →</a>'
        pager += "</div>"
    import_line = ""
    if imported is not None:
        import_line = (f'<p class="okline">✓ Imported {imported} link(s), '
                       f'skipped {skipped or 0} already known.</p>')
    enrich_block = ""
    enrich_state = enrich or {}
    if enrich_state.get("running"):
        phase_label = ("🔎 Scanning your saved posts… " if enrich_state.get("phase") == "saved-scan"
                       else "⏳ Looking up on Reddit… ")
        enrich_block = f"""
<div class="infobox">
  <p><span id="enrich-label">{phase_label}</span><span id="enrich-counts">{enrich_state.get("processed", 0)}/{enrich_state.get("total", 0)}, {enrich_state.get("found", 0)} found</span></p>
  <button class="bigbtn" disabled>✨ Enrich missing info</button>
</div>
<script>
(function () {{
  var iv = setInterval(function () {{
    fetch('/api/enrich-status').then(function (r) {{ return r.json(); }}).then(function (s) {{
      var el = document.getElementById('enrich-counts');
      var lb = document.getElementById('enrich-label');
      if (el) el.textContent = s.processed + '/' + s.total + ', ' + s.found + ' found';
      if (lb) lb.textContent = s.phase === 'saved-scan' ? '🔎 Scanning your saved posts… ' : '⏳ Looking up on Reddit… ';
      if (!s.running) {{ clearInterval(iv); location.reload(); }}
    }}).catch(function () {{}});
  }}, 5000);
}})();
</script>"""
    elif missing:
        enrich_block = f"""
<form method="post" action="/history/enrich" class="form">
  <button type="submit" class="bigbtn">✨ Enrich missing info ({missing})</button>
  <span class="dim">Two passes: first looks each link up on Reddit, then it scans your
  full saved list to match whatever's left — one request per 1.2s, in the background.
  Existing titles are never overwritten, and no new links are added.</span>
</form>"""
    body = f"""
<h1>History</h1>
{import_line}
{enrich_block}
<form method="get" action="/history" class="searchbar">
  <input type="search" name="q" value="{esc(q)}" placeholder="Search title, URL, author, subreddit…">
  <button type="submit">Search</button>
</form>
<table class="tbl">
<tr><th>Status</th><th>Link</th><th>Author</th><th>Subreddit</th><th>Source</th><th>Sent</th></tr>
{''.join(rows) if rows else '<tr><td colspan="6" class="dim">Nothing here yet.</td></tr>'}
</table>
{pager}
<h2>Import history</h2>
<p class="dim">Upload a <span class="mono">sent_links.txt</span> (one URL per line) to
backfill the database — e.g. from the old standalone script. Duplicates are
skipped.</p>
<form method="post" action="/history/import" enctype="multipart/form-data" class="form">
  <input type="file" name="file" accept=".txt,text/plain" required>
  <button type="submit" class="bigbtn">Import</button>
</form>
"""
    return layout("History", body, "history")


def runs_page(runs) -> str:
    rows = []
    for r in runs:
        rows.append(
            f"<tr><td class='nowrap'>{fmt_dt(r.started_at)}</td>"
            f"<td>{status_badge(r.status)}</td>"
            f"<td>{r.links_found}</td><td>{r.links_new}</td><td>{r.links_sent}</td>"
            f"<td><a href='/runs/{r.id}'>detail</a></td></tr>"
        )
    body = f"""
<h1>Runs</h1>
<table class="tbl">
<tr><th>Started</th><th>Status</th><th>Found</th><th>New</th><th>Sent</th><th></th></tr>
{''.join(rows) if rows else '<tr><td colspan="6" class="dim">No runs yet.</td></tr>'}
</table>
"""
    return layout("Runs", body, "runs")


def run_detail_page(run, links) -> str:
    rows = []
    for link in links:
        rows.append(
            f"<tr><td>{status_badge(link.status)}</td>"
            f"<td>{esc(link.post_title or '(no title)')}<br><span class='dim mono'>{esc(link.url[:90])}</span></td>"
            f"<td>{esc(link.subreddit or '—')}</td></tr>"
        )
    body = f"""
<h1>Run #{run.id}</h1>
<div class="cards">
  <div class="card"><div class="k">Status</div><div class="v">{status_badge(run.status)}</div></div>
  <div class="card"><div class="k">Started</div><div class="v small">{fmt_dt(run.started_at)}</div></div>
  <div class="card"><div class="k">Finished</div><div class="v small">{fmt_dt(run.finished_at)}</div></div>
  <div class="card"><div class="k">Found / New / Sent</div><div class="v">{run.links_found} / {run.links_new} / {run.links_sent}</div></div>
</div>
{f'<p class="warn">⚠️ {esc(run.error)}</p>' if run.error else ''}
<h2>Links in this run</h2>
<table class="tbl">
<tr><th>Status</th><th>Link</th><th>Subreddit</th></tr>
{''.join(rows) if rows else '<tr><td colspan="3" class="dim">No links recorded for this run.</td></tr>'}
</table>
<p><a href="/runs">← All runs</a></p>
"""
    return layout(f"Run #{run.id}", body, "runs")


def settings_page(s: dict, saved: bool) -> str:
    def checked(key):
        return "checked" if s.get(key) == "1" else ""

    saved_line = '<p class="okline">✓ Settings saved — schedule re-armed.</p>' if saved else ""
    body = f"""
<h1>Settings</h1>
{saved_line}
<form method="post" action="/settings" class="form">
  <h2>Schedule</h2>
  <p class="dim">Two scans a day, container-local time (set <span class="mono">TZ</span> in your compose file).</p>
  <label>First scan <input type="time" name="scan_time_1" value="{esc(s.get('scan_time_1', '08:00'))}" required></label>
  <label>Second scan <input type="time" name="scan_time_2" value="{esc(s.get('scan_time_2', '20:00'))}" required></label>
  <h2>Sources</h2>
  <label>Posts per source <input type="number" name="post_limit" value="{esc(s.get('post_limit', '25'))}" min="1" max="100" required></label>
  <p class="dim">How many posts to check per source each scan (Reddit's per-request max is 100).</p>
  <label class="check"><input type="checkbox" name="include_upvoted" value="1" {checked('include_upvoted')}> Also scan my upvoted posts</label>
  <p class="dim">Upvoted posts need your Reddit token to have the <span class="mono">history</span> scope.
  If it doesn't, scans keep working on saved posts and you'll get a warning here with re-authorize steps.</p>
  <h2>Notifications</h2>
  <label class="check"><input type="checkbox" name="notify_results" value="1" {checked('notify_results')}> Per-run result pings (sent / no new links / failures)</label>
  <label class="check"><input type="checkbox" name="notify_watchdog" value="1" {checked('notify_watchdog')}> Reddit auth watchdog alerts (every 3rd consecutive failure)</label>
  <p class="dim">Credentials (Reddit, JDownloader, Pushover) come from environment variables only — they are never shown or stored here.</p>
  <button type="submit" class="bigbtn">Save</button>
</form>
"""
    return layout("Settings", body, "settings")


def logs_view() -> str:
    body = """
<h1>Logs</h1>
<p class="dim">Last 300 lines of <span class="mono">redsaves.log</span> (newest at bottom).
The log rotates at 1 MB — the previous chunk is kept as <span class="mono">redsaves.log.1</span>.</p>
<div class="logbar">
  <button id="log-refresh" class="btn">Refresh now</button>
  <span class="dim">auto-refreshes every 15s</span>
</div>
<pre id="logview" class="logview">loading…</pre>
<script>
async function loadLogs() {
  try {
    const r = await fetch('/api/logs');
    const d = await r.json();
    const el = document.getElementById('logview');
    el.textContent = d.lines.length ? d.lines.join('\\n') : '(log is empty)';
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    document.getElementById('logview').textContent = 'could not load log: ' + e;
  }
}
document.getElementById('log-refresh').addEventListener('click', loadLogs);
loadLogs();
setInterval(loadLogs, 15000);
</script>
"""
    return layout("Logs", body, "logs")


def top_page(subreddits, authors, weekly) -> str:
    def rank_rows(items, prefix=""):
        rows = []
        for i, (name, count) in enumerate(items, 1):
            rows.append(
                f"<tr><td class='dim'>#{i}</td><td>{prefix}{esc(name)}</td>"
                f"<td>{count}</td></tr>"
            )
        return "".join(rows) or '<tr><td colspan="3" class="dim">Nothing yet.</td></tr>'

    max_week = max((c for _, c in weekly), default=0)
    bars = []
    for label, count in weekly:
        pct = round(count / max_week * 100) if max_week else 0
        bars.append(
            f"<div class='bar-row'><span class='bar-label'>{esc(label)}</span>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{pct}%'></div></div>"
            f"<span class='bar-count'>{count}</span></div>"
        )
    body = f"""
<h1>Top</h1>
<div class="topgrid">
  <div>
    <h2>Top subreddits</h2>
    <table class="tbl">
    <tr><th></th><th>Subreddit</th><th>Links sent</th></tr>
    {rank_rows(subreddits, prefix="r/")}
    </table>
  </div>
  <div>
    <h2>Top posters</h2>
    <table class="tbl">
    <tr><th></th><th>Author</th><th>Links sent</th></tr>
    {rank_rows(authors, prefix="u/")}
    </table>
  </div>
</div>
<h2>Links sent per week</h2>
<p class="dim">Last 8 weeks, sent links only.</p>
<div class="bars">
{''.join(bars)}
</div>
"""
    return layout("Top", body, "top")


def stats_page(ctx: dict) -> str:
    """Collection stats: totals, pics vs clips, playtime, top lists, growth."""
    def rank_rows(items, prefix=""):
        rows = []
        for i, (name, count) in enumerate(items, 1):
            rows.append(
                f"<tr><td class='dim'>#{i}</td><td>{prefix}{esc(name)}</td>"
                f"<td>{count}</td></tr>"
            )
        return "".join(rows) or '<tr><td colspan="3" class="dim">Nothing yet.</td></tr>'

    max_month = max((c for _, c in ctx.get("monthly", [])), default=0)
    bars = []
    for label, count in ctx.get("monthly", []):
        pct = round(count / max_month * 100) if max_month else 0
        bars.append(
            f"<div class='bar-row'><span class='bar-label'>{esc(label)}</span>"
            f"<div class='bar-track'><div class='bar-fill' style='width:{pct}%'></div></div>"
            f"<span class='bar-count'>{count}</span></div>"
        )
    body = f"""
<h1>Stats</h1>
<div class="cards">
  <div class="card"><div class="k">Links</div><div class="v">{ctx.get("total", 0)}</div></div>
  <div class="card"><div class="k">🎬 Clips</div><div class="v">{ctx.get("clips", 0)}</div></div>
  <div class="card"><div class="k">🖼️ Pics</div><div class="v">{ctx.get("pics", 0)}</div></div>
  <div class="card"><div class="k">★ Favorites</div><div class="v">{ctx.get("favs", 0)}</div></div>
  <div class="card"><div class="k">💀 Dead</div><div class="v">{ctx.get("dead", 0)}</div></div>
  <div class="card"><div class="k">📁 On disk</div><div class="v">{ctx.get("local", 0)}</div></div>
  <div class="card"><div class="k">▶️ Playtime</div><div class="v small">{esc(ctx.get("playtime", "—"))}</div></div>
  <div class="card"><div class="k">👁️ Views</div><div class="v">{ctx.get("views", 0):,}</div></div>
</div>
<div class="topgrid">
  <div>
    <h2>Top posters</h2>
    <table class="tbl">
    <tr><th></th><th>Author</th><th>Links</th></tr>
    {rank_rows(ctx.get("top_authors", []), prefix="u/")}
    </table>
  </div>
  <div>
    <h2>Top subreddits</h2>
    <table class="tbl">
    <tr><th></th><th>Subreddit</th><th>Links</th></tr>
    {rank_rows(ctx.get("top_subs", []), prefix="r/")}
    </table>
  </div>
</div>
<div class="topgrid">
  <div>
    <h2>Top creators</h2>
    <table class="tbl">
    <tr><th></th><th>Creator</th><th>Links</th></tr>
    {rank_rows(ctx.get("top_creators", []), prefix="@")}
    </table>
  </div>
  <div>
    <h2>Links added per month</h2>
    <p class="dim">Last 12 months.</p>
    <div class="bars">
    {''.join(bars)}
    </div>
  </div>
</div>
"""
    return layout("Stats", body, "stats")


def playlists_page(playlists) -> str:
    """List of playlists with item counts, create + delete."""
    rows = []
    for pl, count in playlists:
        rows.append(
            f"<tr><td><a href='/playlists/{pl.id}'>{esc(pl.name)}</a></td>"
            f"<td>{count}</td>"
            f"<td class='dim'>{fmt_dt(pl.created_at)}</td>"
            f"<td><form method='post' action='/playlists/{pl.id}/delete' "
            f"onsubmit=\"return confirm('Delete playlist {esc(pl.name)}? "
            f"The links stay in your library.')\">"
            f"<button type='submit' class='btn danger'>Delete</button></form></td></tr>"
        )
    body = f"""
<h1>Playlists</h1>
<p class="dim">Named collections — group videos and pics however you like.
Deleting a playlist never deletes the links themselves.</p>
<table class="tbl">
<tr><th>Name</th><th>Items</th><th>Created</th><th></th></tr>
{''.join(rows) if rows else '<tr><td colspan="4" class="dim">No playlists yet — make one below.</td></tr>'}
</table>
<h2>New playlist</h2>
<form method="post" action="/playlists" class="form">
  <label>Name <input type="text" name="name" maxlength="128" required
         placeholder="e.g. favorites of the month"></label>
  <button type="submit" class="bigbtn">Create</button>
</form>
<p class="dim">Tip: open anything in the Media center and use the
<span class="mono">＋ List</span> button in the player to add it to a playlist.</p>
"""
    return layout("Playlists", body, "playlists")


def playlist_detail_page(playlist, links) -> str:
    """Items in one playlist, with remove buttons."""
    rows = []
    for link in links:
        title = esc(link.post_title or link.url)
        rows.append(
            f"<tr><td>{title}<br><span class='dim mono'>{esc(link.url[:80])}</span></td>"
            f"<td>{esc(link.author or '—')}</td>"
            f"<td><a href='/media?q={esc(link.url[:40])}'>open in Media ↗</a></td>"
            f"<td><form method='post' action='/api/playlists/{playlist.id}/remove/{link.id}'>"
            f"<button type='submit' class='btn'>Remove</button></form></td></tr>"
        )
    body = f"""
<h1>{esc(playlist.name)}</h1>
<p class="dim">{len(links)} item{'s' if len(links) != 1 else ''}</p>
<table class="tbl">
<tr><th>Link</th><th>Author</th><th></th><th></th></tr>
{''.join(rows) if rows else '<tr><td colspan="4" class="dim">Empty — add things from the Media center.</td></tr>'}
</table>
<p><a href="/playlists">← All playlists</a></p>
"""
    return layout(playlist.name, body, "playlists")


def dupes_page(groups) -> str:
    """Suspected duplicate links, grouped, with per-group merge."""
    blocks = []
    for n, g in enumerate(groups):
        cards = []
        for link in g["links"]:
            keep = " <b>← keep</b>" if link.id == g["winner_id"] else ""
            title = esc(link.post_title or link.url)
            cards.append(
                f"<tr><td>{title}<br><span class='dim mono'>{esc(link.url[:80])}</span></td>"
                f"<td class='dim'>{esc(link.source or '')}</td>"
                f"<td>{'★' if link.favorite else ''}{'📁' if link.local_file else ''}{keep}</td></tr>"
            )
        losers = ",".join(str(l.id) for l in g["links"] if l.id != g["winner_id"])
        blocks.append(f"""
<div class="dupegroup">
  <h3>Dupe group {n + 1} <span class="dim">({esc(g["reason"])})</span></h3>
  <table class="tbl">
  <tr><th>Link</th><th>Source</th><th></th></tr>
  {''.join(cards)}
  </table>
  <form method="post" action="/dupes/merge" class="form">
    <input type="hidden" name="winner_id" value="{g["winner_id"]}">
    <input type="hidden" name="loser_ids" value="{losers}">
    <button type="submit" class="btn">Merge — keep the marked one, delete {len(g["links"]) - 1}</button>
    <span class="dim">The winner is picked automatically: ★ favorite first,
    then downloaded, then most info. Deleting here only removes library rows —
    JDownloader downloads are untouched.</span>
  </form>
</div>""")
    merge_all = ""
    if groups:
        merge_all = """
<form method="post" action="/dupes/merge-all" class="form"
      onsubmit="return confirm('Merge ALL dupe groups, keeping the auto-picked winner of each?')">
  <button type="submit" class="bigbtn">Merge all groups</button>
</form>"""
    body = f"""
<h1>Dupes</h1>
<p class="dim">The same post saved twice — e.g. once as a redgifs link and once
as v.redd.it — or the same file under two URL forms. {len(groups)} group{'s' if len(groups) != 1 else ''} found.</p>
{merge_all}
{''.join(blocks) if blocks else '<p class="okline">✓ No dupes — nice and clean.</p>'}
"""
    return layout("Dupes", body, "media")


def graveyard_page(links, deleted=None, skipped=None) -> str:
    """Dead links, with bulk purge."""
    rows = []
    for link in links:
        title = esc(link.post_title or link.url)
        kept = ""
        if (link.local_file or "") or (link.jd_status or "") == "done":
            kept = ' <span class="mbadge local">📁 kept</span>'
        rows.append(
            f"<tr><td>{title}{kept}<br><span class='dim mono'>{esc(link.url[:80])}</span></td>"
            f"<td>{esc(link.author or '—')}</td>"
            f"<td class='dim nowrap'>{fmt_dt(link.checked_at)}</td>"
            f"<td><form method='post' action='/graveyard/delete/{link.id}'>"
            f"<button type='submit' class='btn'>Delete</button></form></td></tr>"
        )
    purge = ""
    if links:
        purge = f"""
<form method="post" action="/graveyard/purge" class="form"
      onsubmit="return confirm('Delete dead links from the library? Rows with a downloaded file are always kept.')">
  <button type="submit" class="bigbtn danger">🪦 Purge dead ({len(links)})</button>
  <span class="dim">Removes the library rows only — and never a row with a
  matched local file or a finished JDownloader download. Files on disk are
  untouched either way.</span>
</form>"""
    result_line = ""
    if deleted is not None:
        result_line = (f'<p class="okline">✓ Purged {deleted} dead link(s).'
                       + (f' Kept {skipped} with downloaded files.' if skipped else '')
                       + '</p>')
    body = f"""
<h1>Graveyard</h1>
<p class="dim">Links the dead-link check found gone (deleted posts, pulled videos).
{len(links)} total. Run <b>🩺 Check dead links</b> on the Media page to refresh this list.</p>
{result_line}
{purge}
<table class="tbl">
<tr><th>Link</th><th>Author</th><th>Checked</th><th></th></tr>
{''.join(rows) if rows else '<tr><td colspan="4" class="dim">No dead links. 🎉</td></tr>'}
</table>
"""
    return layout("Graveyard", body, "media")


LB_JS = """
const MEDIA = __MEDIA_JSON__;
const PLAYLISTS = __PLAYLISTS_JSON__;
let lbIndex = -1;
const lb = () => document.getElementById('lightbox');
function fmtNum(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return '' + n;
}
function playerHTML(it) {
  if (it.kind === 'redgifs')
    return '<iframe src="' + it.src + '" frameborder="0" scrolling="no" allowfullscreen style="width:100%;height:100%"></iframe>';
  if (it.kind === 'video')
    return '<video src="' + it.src + '" controls autoplay loop playsinline style="width:100%;height:100%"></video>';
  if (it.kind === 'image')
    return '<img src="' + it.src + '" style="max-width:100%;max-height:100%">';
  return '<p class="dim">No inline player for this link.</p>';
}
function renderLb() {
  const it = MEDIA[lbIndex];
  document.getElementById('lb-player').innerHTML = playerHTML(it);
  document.getElementById('lb-title').textContent = it.title;
  const bits = [];
  if (it.author) bits.push(it.author);
  if (it.creator && it.creator !== it.author) bits.push('@' + it.creator);
  if (it.sub) bits.push('r/' + it.sub);
  if (it.score) bits.push('▲ ' + it.score);
  if (it.views) bits.push('👁 ' + fmtNum(it.views));
  if (it.likes) bits.push('♥ ' + fmtNum(it.likes));
  if (it.ratio) bits.push(Math.round(it.ratio * 100) + '% upvoted');
  if (it.flair) bits.push('🏷️ ' + it.flair);
  if (it.jd && it.jd !== 'done') bits.push('JD: ' + it.jd);
  if (it.duration) bits.push(fmtDur(it.duration));
  document.getElementById('lb-sub').textContent = bits.join(' · ');
  document.getElementById('lb-tags').textContent = it.tags ? '#' + it.tags.split(', ').slice(0, 8).join(' #') : '';
  document.getElementById('lb-open').href = it.url;
  document.getElementById('lb-note').textContent =
    (it.kind === 'redgifs' && !it.local)
      ? '⚠️ redgifs embeds are blocked in your region and this one isn\'t downloaded — run “Scan local files” after your next JDownloader batch.'
      : '';
  document.getElementById('lb-fav').textContent = it.fav ? '★' : '☆';
  document.getElementById('lb-fav').classList.toggle('on', !!it.fav);
  const v = document.querySelector('#lb-player video');
  if (v) v.onended = () => lbNav(1);
}
function fmtDur(s) {
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return m + ':' + String(r).padStart(2, '0');
}
function openLb(i) {
  if (!MEDIA.length) return;
  lbIndex = ((i % MEDIA.length) + MEDIA.length) % MEDIA.length;
  renderLb();
  lb().hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeLb() {
  lb().hidden = true;
  document.getElementById('lb-player').innerHTML = '';
  document.body.style.overflow = '';
  lbIndex = -1;
}
function lbNav(d) { openLb(lbIndex + d); }
function toggleFav(id, btn) {
  fetch('/api/favorite/' + id, {method: 'POST'})
    .then(r => r.json()).then(s => {
      MEDIA.forEach(m => { if (m.id === id) m.fav = s.favorite; });
      document.querySelectorAll('.mstar[data-id="' + id + '"]').forEach(b => {
        b.textContent = s.favorite ? '★' : '☆';
        b.classList.toggle('on', s.favorite);
      });
      const it = MEDIA[lbIndex];
      if (it && it.id === id) renderLb();
    });
}
document.addEventListener('click', e => {
  const star = e.target.closest('.mstar');
  if (star) { e.stopPropagation(); toggleFav(+star.dataset.id, star); return; }
  const card = e.target.closest('.mcard');
  if (card) {
    if (card.dataset.kind === 'link') { window.open(card.dataset.url, '_blank'); return; }
    openLb(+card.dataset.idx);
  }
});
document.addEventListener('mouseover', e => {
  const v = e.target.closest('.mcard video');
  if (v) v.play().catch(() => {});
});
document.addEventListener('mouseout', e => {
  const v = e.target.closest('.mcard video');
  if (v) { v.pause(); }
});
document.addEventListener('keydown', e => {
  if (lb().hidden) return;
  if (e.key === 'Escape') closeLb();
  if (e.key === 'ArrowRight') lbNav(1);
  if (e.key === 'ArrowLeft') lbNav(-1);
});
document.getElementById('mshuffle').addEventListener('click', () => {
  openLb(Math.floor(Math.random() * MEDIA.length));
});
function fillPlaylistSelect() {
  const sel = document.getElementById('lb-pl');
  if (!sel) return;
  sel.innerHTML = PLAYLISTS.map(p =>
    '<option value="' + p.id + '">' + p.name.replace(/</g, '&lt;') + '</option>').join('');
}
function addToPlaylist() {
  const sel = document.getElementById('lb-pl');
  const it = MEDIA[lbIndex];
  if (!sel || !sel.value || !it) return;
  fetch('/api/playlists/' + sel.value + '/add/' + it.id, {method: 'POST'})
    .then(r => r.json()).then(s => {
      const btn = document.getElementById('lb-pl-add');
      btn.textContent = s.ok ? '✓ Added' : '＋ List';
      setTimeout(() => { btn.textContent = '＋ List'; }, 1500);
    });
}
fillPlaylistSelect();
"""


def media_page(links, q="", author="", subreddit="", creator="", fav=0,
               hide_dead=0, sort="new", authors=(), subs=(), creators=(),
               enrich=None, linkcheck=None, filescan=None, jdsync=None,
               playlists=(), dupe_count=0, dead_count=0) -> str:
    """Media center: filterable video grid + lightbox player."""
    cards = []
    js_items = []
    for n, link in enumerate(links):
        kind, src = media_embed(link.url, link.id, link.local_file or "")
        local = bool(link.local_file)
        title = link.post_title or link.url
        short_title = title if len(title) <= 80 else title[:77] + "..."
        # real thumbnail on top when we have one; falls away if it 404s
        thumb = (f'<img class="mthumb" loading="lazy" src="{esc(link.thumbnail)}" '
                 f'onerror="this.remove()" alt="">' if link.thumbnail else '')
        if kind == "redgifs":
            preview = (f'<iframe loading="lazy" src="{esc(src)}" frameborder="0" '
                       f'scrolling="no" tabindex="-1"></iframe><span class="mplay">▶</span>')
        elif kind == "video":
            preview = (f'<video muted loop playsinline preload="metadata" '
                       f'src="{esc(src)}"></video><span class="mplay">▶</span>')
        elif kind == "image":
            preview = f'<img loading="lazy" src="{esc(src)}" alt="">'
        else:
            preview = '<span class="mplay">↗</span>'
        star = "★" if link.favorite else "☆"
        star_cls = "mstar on" if link.favorite else "mstar"
        badges = []
        if local:
            badges.append('<span class="mbadge local">📁 local</span>')
        if link.duration:
            m, s = divmod(int(link.duration), 60)
            badges.append(f'<span class="mbadge">{m}:{s:02d}</span>')
        if link.score:
            badges.append(f'<span class="mbadge">▲ {link.score}</span>')
        if link.flair:
            badges.append(f'<span class="mbadge flair">🏷️ {esc(link.flair)}</span>')
        jd = link.jd_status or ""
        if jd == "downloading":
            badges.append('<span class="mbadge jd">⬇ downloading</span>')
        elif jd == "queued":
            badges.append('<span class="mbadge jd">⏳ queued</span>')
        elif jd == "failed":
            badges.append('<span class="mbadge dead">✗ JD failed</span>')
        if not link.alive:
            badges.append('<span class="mbadge dead">💀 dead</span>')
        meta_bits = [b for b in (link.author, link.creator, link.subreddit) if b]
        cards.append(f"""
<article class="mcard{' isdead' if not link.alive else ''}" data-idx="{n}" data-kind="{kind}" data-url="{esc(link.url)}">
  <div class="mprev">{thumb}{preview}
    <button class="{star_cls}" data-id="{link.id}" title="Toggle favorite">{star}</button>
    <div class="mbadges">{''.join(badges)}</div>
  </div>
  <div class="mmeta">
    <p class="mtitle">{esc(short_title)}</p>
    <p class="msub">{" · ".join(esc(b) for b in meta_bits)}</p>
  </div>
</article>""")
        js_items.append({"id": link.id, "kind": kind, "src": src, "url": link.url,
                         "title": title, "author": link.author or "",
                         "creator": link.creator or "", "sub": link.subreddit or "",
                         "fav": bool(link.favorite), "score": link.score or 0,
                         "duration": link.duration or 0, "local": local,
                         "views": link.views or 0, "likes": link.likes or 0,
                         "tags": link.tags or "", "flair": link.flair or "",
                         "ratio": link.upvote_ratio or 0,
                         "jd": link.jd_status or ""})

    def opts(pairs, current, label):
        out = [f'<option value="">{label}</option>']
        for name, count in pairs:
            sel = " selected" if name == current else ""
            out.append(f'<option value="{esc(name)}"{sel}>{esc(name)} ({count})</option>')
        return "\n".join(out)

    enrich_state = enrich or {}
    lc_state = linkcheck or {}
    fs_state = filescan or {}
    jd_state = jdsync or {}
    tools = ""
    if jd_state.get("running"):
        tools += (f'<div class="infobox"><p>🔄 Syncing JDownloader status… '
                  f'<span id="jd-counts">{jd_state.get("processed", 0)}/'
                  f'{jd_state.get("total", 0)}, {jd_state.get("done", 0)} done, '
                  f'{jd_state.get("failed", 0)} failed</span></p></div>')
    if enrich_state.get("running"):
        phase = enrich_state.get("phase")
        label = ("🔎 Scanning saved posts… " if phase == "saved-scan"
                 else "↻ Refreshing metadata… " if phase == "refresh"
                 else "⏳ Looking up on Reddit… ")
        tools += (f'<div class="infobox"><p>{label}'
                  f'<span id="enrich-counts">{enrich_state.get("processed", 0)}/'
                  f'{enrich_state.get("total", 0)}, {enrich_state.get("found", 0)} found</span></p></div>')
    if lc_state.get("running"):
        tools += (f'<div class="infobox"><p>🩺 Checking links… '
                  f'<span id="lc-counts">{lc_state.get("processed", 0)}/'
                  f'{lc_state.get("total", 0)}, {lc_state.get("dead", 0)} dead</span></p></div>')
    if fs_state.get("running"):
        tools += (f'<div class="infobox"><p>📁 Matching local files… '
                  f'<span id="fs-counts">{fs_state.get("processed", 0)}/'
                  f'{fs_state.get("total", 0)}, {fs_state.get("matched", 0)} matched</span></p></div>')
    tools += """
<script>
(function () {
  var iv = setInterval(function () {
    var jobs = [
      ['/api/enrich-status', 'enrich-counts',
       s => s.processed + '/' + s.total + ', ' + s.found + ' found'],
      ['/api/linkcheck-status', 'lc-counts',
       s => s.processed + '/' + s.total + ', ' + s.dead + ' dead'],
      ['/api/filescan-status', 'fs-counts',
       s => s.processed + '/' + s.total + ', ' + s.matched + ' matched'],
      ['/api/jdsync-status', 'jd-counts',
       s => s.processed + '/' + s.total + ', ' + s.done + ' done, ' + s.failed + ' failed']
    ];
    var waiting = jobs.length, anyRunning = false, anyBox = false;
    jobs.forEach(function (job) {
      fetch(job[0]).then(r => r.json()).then(s => {
        var el = document.getElementById(job[1]);
        if (el) { anyBox = true; el.textContent = job[2](s); if (s.running) anyRunning = true; }
      }).catch(() => {}).finally(() => {
        if (--waiting === 0 && anyBox && !anyRunning) { clearInterval(iv); location.reload(); }
      });
    });
  }, 5000);
})();
</script>"""

    body = f"""
<h1>Media</h1>
<form method="get" action="/media" class="mediabar">
  <input type="search" name="q" value="{esc(q)}" placeholder="Search title, author…">
  <select name="author">{opts(authors, author, "All posters")}</select>
  <select name="creator">{opts(creators, creator, "All creators")}</select>
  <select name="subreddit">{opts(subs, subreddit, "All subreddits")}</select>
  <select name="sort">
    <option value="new"{' selected' if sort != 'old' else ''}>Newest first</option>
    <option value="old"{' selected' if sort == 'old' else ''}>Oldest first</option>
  </select>
  <label class="favcheck"><input type="checkbox" name="fav" value="1"{' checked' if fav else ''}> ★ only</label>
  <label class="favcheck"><input type="checkbox" name="hide_dead" value="1"{' checked' if hide_dead else ''}> Hide dead</label>
  <button type="submit" class="btn">Filter</button>
  <button type="button" class="btn" id="mshuffle">🔀 Shuffle</button>
</form>
<div class="mediatools">
  <form method="post" action="/media/refresh">
    <button type="submit" class="btn"{" disabled" if enrich_state.get("running") else ""}>↻ Refresh metadata</button>
  </form>
  <form method="post" action="/media/check-links">
    <button type="submit" class="btn"{" disabled" if lc_state.get("running") else ""}>🩺 Check dead links</button>
  </form>
  <form method="post" action="/media/scan-files">
    <button type="submit" class="btn"{" disabled" if fs_state.get("running") else ""}>📁 Scan local files</button>
  </form>
  <form method="post" action="/media/sync-jd">
    <button type="submit" class="btn"{" disabled" if jd_state.get("running") else ""}>🔄 Sync JD status</button>
  </form>
  <a class="btn" href="/dupes">🧹 Dupes{f' ({dupe_count})' if dupe_count else ''}</a>
  <a class="btn" href="/graveyard">🪦 Graveyard{f' ({dead_count})' if dead_count else ''}</a>
  <span class="dim">Refresh pulls thumbnails, scores and durations from your saved posts. Dead-link check verifies every URL. Scan local files matches your JDownloader downloads so videos play from disk — no regional blocks. All run in the background.</span>
</div>
{tools}
<p class="dim">{len(links)} item{'s' if len(links) != 1 else ''}</p>
<div class="mgrid">
{''.join(cards) if cards else '<p class="dim">Nothing here — try clearing the filters.</p>'}
</div>
<div id="lightbox" class="lb" hidden>
  <div class="lb-backdrop" onclick="closeLb()"></div>
  <div class="lb-box">
    <div id="lb-player" class="lb-player"></div>
    <div class="lb-meta">
      <h3 id="lb-title"></h3>
      <p id="lb-sub" class="dim"></p>
      <p id="lb-tags" class="dim"></p>
      <p id="lb-note" class="dim"></p>
      <div class="lb-actions">
        <button class="btn" onclick="lbNav(-1)">◀</button>
        <button class="btn" onclick="lbNav(1)">▶</button>
        <button class="btn" id="lb-fav" onclick="toggleFav(MEDIA[lbIndex].id)">☆</button>
        <select id="lb-pl" class="btn" title="Add to playlist"></select>
        <button class="btn" id="lb-pl-add" onclick="addToPlaylist()">＋ List</button>
        <a class="btn" id="lb-open" target="_blank" rel="noopener">Open original ↗</a>
        <button class="btn" onclick="closeLb()">✕</button>
      </div>
    </div>
  </div>
</div>
<script>{LB_JS.replace("__MEDIA_JSON__", json.dumps(js_items)).replace("__PLAYLISTS_JSON__", json.dumps([{"id": p.id, "name": p.name} for p in playlists]))}</script>
"""
    return layout("Media", body, active="media")
