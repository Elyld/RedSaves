"""Server-rendered HTML pages for RedSaves."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape as _esc

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
        enrich_block = f"""
<div class="infobox">
  <p>⏳ Enriching missing info… <span id="enrich-counts">{enrich_state.get("processed", 0)}/{enrich_state.get("total", 0)}, {enrich_state.get("found", 0)} found</span></p>
  <button class="bigbtn" disabled>✨ Enrich missing info</button>
</div>
<script>
(function () {{
  var iv = setInterval(function () {{
    fetch('/api/enrich-status').then(function (r) {{ return r.json(); }}).then(function (s) {{
      var el = document.getElementById('enrich-counts');
      if (el) el.textContent = s.processed + '/' + s.total + ', ' + s.found + ' found';
      if (!s.running) {{ clearInterval(iv); location.reload(); }}
    }}).catch(function () {{}});
  }}, 5000);
}})();
</script>"""
    elif missing:
        enrich_block = f"""
<form method="post" action="/history/enrich" class="form">
  <button type="submit" class="bigbtn">✨ Enrich missing info ({missing})</button>
  <span class="dim">Looks up titles, authors and subreddits on Reddit — one request
  per 1.2s, in the background. Existing titles are never overwritten.</span>
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
