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


LB_JS = """
const MEDIA = __MEDIA_JSON__;
let lbIndex = -1;
const lb = () => document.getElementById('lightbox');
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
  if (it.duration) bits.push(fmtDur(it.duration));
  document.getElementById('lb-sub').textContent = bits.join(' · ');
  document.getElementById('lb-open').href = it.url;
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
"""


def media_page(links, q="", author="", subreddit="", creator="", fav=0,
               hide_dead=0, sort="new", authors=(), subs=(), creators=(),
               enrich=None, linkcheck=None) -> str:
    """Media center: filterable video grid + lightbox player."""
    cards = []
    js_items = []
    for n, link in enumerate(links):
        kind, src = media_embed(link.url)
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
        if link.duration:
            m, s = divmod(int(link.duration), 60)
            badges.append(f'<span class="mbadge">{m}:{s:02d}</span>')
        if link.score:
            badges.append(f'<span class="mbadge">▲ {link.score}</span>')
        if not link.alive:
            badges.append('<span class="mbadge dead">💀 dead</span>')
        meta_bits = [b for b in (link.author, link.creator, link.subreddit) if b]
        cards.append(f"""
<article class="mcard{' isdead' if not link.alive else ''}" data-idx="{n}" data-kind="{kind}" data-url="{esc(link.url)}">
  <div class="mprev">{thumb}{preview}
    <button class="{star_cls}" data-id="{link.id}" title="Toggle favorite">{star}</button>
    {''.join(badges)}
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
                         "duration": link.duration or 0})

    def opts(pairs, current, label):
        out = [f'<option value="">{label}</option>']
        for name, count in pairs:
            sel = " selected" if name == current else ""
            out.append(f'<option value="{esc(name)}"{sel}>{esc(name)} ({count})</option>')
        return "\n".join(out)

    enrich_state = enrich or {}
    lc_state = linkcheck or {}
    tools = ""
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
    tools += """
<script>
(function () {
  var iv = setInterval(function () {
    var done = 0;
    fetch('/api/enrich-status').then(r => r.json()).then(s => {
      var el = document.getElementById('enrich-counts');
      if (el && s.running) el.textContent = s.processed + '/' + s.total + ', ' + s.found + ' found';
      if (!s.running) { done++; if (done === 2) { clearInterval(iv); location.reload(); } }
    }).catch(() => {});
    fetch('/api/linkcheck-status').then(r => r.json()).then(s => {
      var el = document.getElementById('lc-counts');
      if (el && s.running) el.textContent = s.processed + '/' + s.total + ', ' + s.dead + ' dead';
      if (!s.running) { done++; if (done === 2) { clearInterval(iv); location.reload(); } }
    }).catch(() => {});
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
  <span class="dim">Refresh pulls thumbnails, scores and durations from your saved posts. Dead-link check verifies every URL (redgifs via their API, rest via HEAD) — runs in the background.</span>
</div>
{tools}
<p class="dim">{len(links)} video{'s' if len(links) != 1 else ''}</p>
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
      <div class="lb-actions">
        <button class="btn" onclick="lbNav(-1)">◀</button>
        <button class="btn" onclick="lbNav(1)">▶</button>
        <button class="btn" id="lb-fav" onclick="toggleFav(MEDIA[lbIndex].id)">☆</button>
        <a class="btn" id="lb-open" target="_blank" rel="noopener">Open original ↗</a>
        <button class="btn" onclick="closeLb()">✕</button>
      </div>
    </div>
  </div>
</div>
<script>{LB_JS.replace("__MEDIA_JSON__", json.dumps(js_items))}</script>
"""
    return layout("Media", body, active="media")
