"""RedSaves scan logic, DB-backed refactor of RedSaves2.0.py (v2.4).

Behavior is deliberately identical to the standalone script, except where
the WebUI settings say otherwise:
- saved posts fetched with ?limit= (post_limit setting, default 25, max 100)
- media match: '.gif', '.mp4', 'redgifs', 'v.redd.it' as URL substrings
- optional second source: upvoted posts (include_upvoted setting, default off)
- within-run URL dedup preserving order, across both sources
- links are marked sent ONLY when JDownloader accepts them (a failed send
  burns nothing; the links are retried on the next run)
- 15s timeouts everywhere, rotating 1 MB log file
- Reddit auth watchdog: consecutive failures counted, Pushover alert every 3rd
- Pushover messages verbatim, including the 'no new links' pings
- same env var names as the script

What changed vs the script: sent_links.txt / auth_failures.txt file I/O is
now SQLite (links / settings tables); every scan writes a runs row; post
title/permalink/subreddit/author/source are captured alongside each URL for
the history UI.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from myjdapi import Myjdapi
from sqlalchemy import func, or_

from app.database import DATA_DIR, get_session
from app.models import Link, Run, Setting

logger = logging.getLogger(__name__)

load_dotenv()

REQUEST_TIMEOUT = 15  # seconds; fail fast instead of hanging

LOG_FILE = DATA_DIR / "redsaves.log"
LOG_MAX_BYTES = 1048576  # 1 MB; rotates to redsaves.log.1 when exceeded

WATCHDOG_THRESHOLD = 3  # Pushover alert every this many consecutive auth failures
K_AUTH_FAILURES = "auth_failure_count"
K_NOTIFY_RESULTS = "notify_results"      # per-run result pings; default on
K_NOTIFY_WATCHDOG = "notify_watchdog"    # every-3rd auth failure alert; default on
K_POST_LIMIT = "post_limit"              # posts fetched per source; default 25
K_INCLUDE_UPVOTED = "include_upvoted"    # also scan upvoted posts; default off
K_UPVOTED_SCOPE_MISSING = "upvoted_scope_missing"  # dashboard warning flag

POST_LIMIT_DEFAULT = 25
POST_LIMIT_MIN = 1
POST_LIMIT_MAX = 100  # Reddit's per-request cap

MEDIA_MARKERS = ('.gif', '.mp4', 'redgifs', 'v.redd.it')

REQUIRED_ENV = ('EMAIL', 'PASSWORD', 'DEVICE_NAME', 'CLIENT_ID', 'SECRET_TOKEN',
                'REFRESH_TOKEN', 'REDDIT_USERNAME', 'DESTINATION_FOLDER',
                'PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN')

_scan_lock = threading.Lock()


# --- Metadata enrichment ----------------------------------------------------
# Backfills title/author/subreddit/permalink for links that have none (e.g.
# imported from a legacy sent_links.txt). Reddit's /api/info?url= returns the
# submissions linking to a URL. One request per 1.2s stays well under Reddit's
# 60-requests/minute OAuth limit.
ENRICH_THROTTLE_SECONDS = 1.2

_enrich_lock = threading.Lock()
_enrich_progress = {"running": False, "processed": 0, "total": 0,
                    "found": 0, "skipped": 0}

K_ENRICH_NOTE = "enrich_note"  # dashboard note set when enrichment aborts


def _throttle():
    """Politeness pause between /api/info requests (mocked in tests)."""
    time.sleep(ENRICH_THROTTLE_SECONDS)


def get_enrich_note() -> str:
    return get_setting_value(K_ENRICH_NOTE, "")


def set_enrich_note(note: str) -> None:
    set_setting_value(K_ENRICH_NOTE, note)


def titleless_count() -> int:
    """How many links have no title yet (enrichment candidates)."""
    session = get_session()
    try:
        return (session.query(func.count(Link.id))
                .filter(or_(Link.post_title == "", Link.post_title.is_(None)))
                .scalar() or 0)
    finally:
        session.close()


def titleless_links() -> list:
    """(id, url) pairs for links missing a title, oldest first."""
    session = get_session()
    try:
        rows = (session.query(Link.id, Link.url)
                .filter(or_(Link.post_title == "", Link.post_title.is_(None)))
                .order_by(Link.id).all())
        return [(r.id, r.url) for r in rows]
    finally:
        session.close()


def get_enrich_progress() -> dict:
    return dict(_enrich_progress)


def _canonicalize_url(url):
    """Normalize a saved media URL to the post form Reddit likely indexed.

    Only used for the /api/info lookup — the stored URL is never rewritten.
    """
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        path = p.path or ""
        if host == "v.redd.it":
            # v.redd.it/{id}/CMAF_1080.mp4 -> v.redd.it/{id}
            segs = [s for s in path.split("/") if s]
            if segs:
                return f"https://v.redd.it/{segs[0]}"
            return url
        if host in ("redgifs.com", "www.redgifs.com", "v3.redgifs.com"):
            # v3.redgifs.com -> www.redgifs.com; /ifr/{slug} -> /watch/{slug}
            segs = [s for s in path.split("/") if s]
            if len(segs) >= 2 and segs[0] == "ifr":
                path = "/watch/" + "/".join(segs[1:])
            return f"https://www.redgifs.com{path}"
        # default: lowercase host, drop query/fragment
        return f"{p.scheme.lower()}://{host}{path}"
    except Exception:
        return url


def _url_variants(url):
    """The canonical post form of the URL plus cheap normalized variants
    for /api/info lookup.

    Reddit indexes the submission URL as-submitted; a saved file often holds
    a slightly different form (www vs bare host, http vs https, media-file
    path on v.redd.it, v3/ifr redgifs forms). Try the canonical form first,
    then the raw URL and scheme/www toggles.
    """
    cands = []
    canonical = _canonicalize_url(url)
    cands.append(canonical)
    if url != canonical:
        cands.append(url)
    if canonical.startswith("https://"):
        cands.append("http://" + canonical[len("https://"):])
    elif canonical.startswith("http://"):
        cands.append("https://" + canonical[len("http://"):])
    out = list(cands)
    for v in cands:
        m = re.match(r"(https?://)(www\.)?(.*)", v)
        if not m:
            continue
        scheme, www, rest = m.groups()
        toggled = scheme + ("" if www else "www.") + rest
        if toggled not in out:
            out.append(toggled)
    seen, uniq = set(), []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:5]


def fetch_info_by_url(config: ScanConfig, token, url):
    """Look up submissions linking to url via /api/info. Tries the URL plus
    normalized variants; returns a metadata dict from the first hit, or None
    when nothing is found / on error (row left untouched)."""
    try:
        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
        for attempt in _url_variants(url):
            response = requests.get('https://oauth.reddit.com/api/info',
                                    params={'url': attempt},
                                    headers=headers, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                log_error(f"/api/info lookup failed for {attempt[:60]}: HTTP {response.status_code}")
                return None
            children = (response.json().get('data') or {}).get('children') or []
            if children:
                if attempt != url:
                    log_info(f"/api/info matched via variant: {attempt[:80]}")
                data = children[0]['data']
                return {
                    'post_title': data.get('title', ''),
                    'post_permalink': data.get('permalink', ''),
                    'subreddit': (data.get('subreddit') or ''),
                    'author': (data.get('author') or ''),
                    'created_utc': data.get('created_utc'),
                }
            log_info(f"/api/info: no match for {url[:80]} (tried {len(_url_variants(url))} forms)")
        return None
    except Exception as e:
        log_error(f"/api/info lookup failed for {url[:60]}: {e}")
        return None


def apply_enrichment(link_id: int, meta: dict) -> None:
    """Fill empty metadata fields on a link. Never overwrites existing
    non-empty values. When the title was empty (an imported bare URL whose
    date is just the import date), stamp sent_at from the post's creation
    date — an approximation, but closer to reality than today."""
    session = get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        if not row:
            return
        title_was_empty = not row.post_title
        if title_was_empty and meta.get('post_title'):
            row.post_title = meta['post_title']
        if not row.post_permalink and meta.get('post_permalink'):
            row.post_permalink = meta['post_permalink']
        if not row.subreddit and meta.get('subreddit'):
            row.subreddit = meta['subreddit']
        if not row.author and meta.get('author'):
            row.author = meta['author']
        if title_was_empty and meta.get('created_utc'):
            try:
                row.sent_at = datetime.fromtimestamp(float(meta['created_utc']))
            except (ValueError, TypeError, OSError):
                pass
        session.commit()
    finally:
        session.close()


def run_enrich(config: ScanConfig | None = None) -> bool:
    """Run metadata enrichment to completion. Returns False immediately when
    another enrichment is already running (never two concurrently)."""
    if not _enrich_lock.acquire(blocking=False):
        log_info("Enrich skipped: another enrichment is already running.")
        return False
    try:
        _run_enrich(config or ScanConfig.from_env())
    finally:
        _enrich_lock.release()
    return True


def _run_enrich(config: ScanConfig | None) -> None:
    global _enrich_progress
    _enrich_progress = {"running": True, "processed": 0, "total": 0,
                        "found": 0, "skipped": 0}
    try:
        set_enrich_note("")  # clear any previous abort note; re-set below if needed
        if config is None:
            msg = "Enrich aborted: missing Reddit credentials in environment."
            log_error(msg)
            set_enrich_note("Metadata enrichment couldn't start — Reddit "
                            "credentials are missing. Check your environment.")
            return
        token = get_access_token(config)
        if not token:
            msg = "Enrich aborted: could not obtain a Reddit access token."
            log_error(msg)
            set_enrich_note("Metadata enrichment couldn't get a Reddit token — "
                            "see the Logs page for details.")
            return
        targets = titleless_links()
        _enrich_progress["total"] = len(targets)
        log_info(f"Enrich started: {len(targets)} link(s) missing titles.")
        for link_id, url in targets:
            meta = fetch_info_by_url(config, token, url)
            if meta:
                apply_enrichment(link_id, meta)
                _enrich_progress["found"] += 1
            else:
                _enrich_progress["skipped"] += 1
            _enrich_progress["processed"] += 1
            _throttle()
        log_info(f"Enrich finished: {_enrich_progress['found']} enriched, "
                 f"{_enrich_progress['skipped']} skipped, "
                 f"of {_enrich_progress['total']} processed.")
    finally:
        _enrich_progress["running"] = False


@dataclass
class ScanConfig:
    email: str
    password: str
    device_name: str
    client_id: str
    secret_token: str
    refresh_token: str
    reddit_username: str
    destination_folder: str
    pushover_user_key: str
    pushover_api_token: str

    @classmethod
    def from_env(cls) -> "ScanConfig | None":
        """None when any required variable is missing (mirrors the script's
        required-env check, without raising at import time)."""
        vals = {k: os.getenv(k) for k in REQUIRED_ENV}
        if not all(vals.values()):
            return None
        return cls(
            email=vals['EMAIL'], password=vals['PASSWORD'],
            device_name=vals['DEVICE_NAME'], client_id=vals['CLIENT_ID'],
            secret_token=vals['SECRET_TOKEN'], refresh_token=vals['REFRESH_TOKEN'],
            reddit_username=vals['REDDIT_USERNAME'],
            destination_folder=vals['DESTINATION_FOLDER'],
            pushover_user_key=vals['PUSHOVER_USER_KEY'],
            pushover_api_token=vals['PUSHOVER_API_TOKEN'],
        )


# --- Logging: console + rotating file ---------------------------------------
def _write_log(line: str) -> None:
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # console already got it; never crash a run over logging


def log_info(message: str) -> None:
    _write_log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: {message}")


def log_error(message: str) -> None:
    _write_log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {message}")


def rotate_log_if_needed() -> None:
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size >= LOG_MAX_BYTES:
            backup = Path(str(LOG_FILE) + ".1")
            if backup.exists():
                backup.unlink()
            LOG_FILE.rename(backup)
            log_info("Log rotated.")
    except OSError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: Log rotation failed: {e}")


# --- Settings helpers --------------------------------------------------------
def get_setting_value(key: str, default: str = "") -> str:
    session = get_session()
    try:
        row = session.query(Setting).filter(Setting.key == key).first()
        return row.value if row else default
    finally:
        session.close()


def set_setting_value(key: str, value: str) -> None:
    session = get_session()
    try:
        row = session.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            session.add(Setting(key=key, value=value))
        session.commit()
    finally:
        session.close()


def notifications_enabled(kind: str) -> bool:
    key = K_NOTIFY_WATCHDOG if kind == "watchdog" else K_NOTIFY_RESULTS
    return get_setting_value(key, "1") == "1"


def get_post_limit() -> int:
    """Configured per-source post limit, clamped to Reddit's 1..100 range."""
    try:
        return max(POST_LIMIT_MIN, min(POST_LIMIT_MAX,
                                      int(get_setting_value(K_POST_LIMIT,
                                                            str(POST_LIMIT_DEFAULT)))))
    except (TypeError, ValueError):
        return POST_LIMIT_DEFAULT


def set_post_limit(value) -> int:
    """Validate + clamp a raw limit value, persist it, return the stored int."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = POST_LIMIT_DEFAULT
    n = max(POST_LIMIT_MIN, min(POST_LIMIT_MAX, n))
    set_setting_value(K_POST_LIMIT, str(n))
    return n


def include_upvoted() -> bool:
    return get_setting_value(K_INCLUDE_UPVOTED, "0") == "1"


def upvoted_scope_missing() -> bool:
    """Dashboard warning flag: the last upvoted fetch failed on auth scope."""
    return get_setting_value(K_UPVOTED_SCOPE_MISSING, "0") == "1"


def set_upvoted_scope_missing(missing: bool) -> None:
    set_setting_value(K_UPVOTED_SCOPE_MISSING, "1" if missing else "0")


# --- Reddit auth watchdog (DB-backed; was auth_failures.txt) -----------------
def get_auth_failure_count() -> int:
    try:
        return int(get_setting_value(K_AUTH_FAILURES, "0") or 0)
    except ValueError:
        return 0


def set_auth_failure_count(n: int) -> None:
    set_setting_value(K_AUTH_FAILURES, str(n))


def reset_auth_failures() -> None:
    if get_auth_failure_count() != 0:
        set_auth_failure_count(0)
        log_info("Reddit auth recovered; failure counter reset.")


def record_auth_failure(pushover=None) -> int:
    count = get_auth_failure_count() + 1
    set_auth_failure_count(count)
    log_error(f"Reddit authentication failed ({count} consecutive failure(s)).")
    if count % WATCHDOG_THRESHOLD == 0 and notifications_enabled("watchdog"):
        (pushover or (lambda m: send_pushover_notification(m, kind="watchdog")))(
            f"RedSaves: Reddit authentication has failed {count} runs in a row. "
            "Your refresh token may have expired - check your .env file."
        )
    return count


# --- Reddit ------------------------------------------------------------------
def get_access_token(config: ScanConfig):
    try:
        auth = requests.auth.HTTPBasicAuth(config.client_id, config.secret_token)
        data = {'grant_type': 'refresh_token', 'refresh_token': config.refresh_token}
        headers = {'User-Agent': 'RedSaves v2.4'}
        response = requests.post('https://www.reddit.com/api/v1/access_token', auth=auth,
                                 data=data, headers=headers, timeout=REQUEST_TIMEOUT)
        return response.json().get('access_token')
    except Exception as e:
        log_error(f"Error obtaining access token: {e}")
        return None


def fetch_saved_posts(config: ScanConfig, token, limit=None):
    limit = get_post_limit() if limit is None else limit
    try:
        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
        response = requests.get(
            f'https://oauth.reddit.com/user/{config.reddit_username}/saved',
            params={'limit': limit},
            headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.json()
        else:
            log_error(f"Failed to fetch saved posts for user '{config.reddit_username}'. "
                      f"Status code: {response.status_code}")
            return None
    except Exception as e:
        log_error(f"Error fetching saved posts: {e}")
        return None


def fetch_upvoted_posts(config: ScanConfig, token, limit=None):
    """Second source: the user's upvoted posts. Returns the listing payload,
    or None when unavailable. A 401/403 means the refresh token lacks the
    `history` OAuth scope: the run continues with saved posts only and a
    dashboard warning flag is set so Josh knows to re-authorize."""
    limit = get_post_limit() if limit is None else limit
    try:
        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
        response = requests.get(
            f'https://oauth.reddit.com/user/{config.reddit_username}/upvoted',
            params={'limit': limit},
            headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            set_upvoted_scope_missing(False)
            return response.json()
        if response.status_code in (401, 403):
            set_upvoted_scope_missing(True)
            log_error("Reddit token lacks the history scope — re-authorize the "
                      "app to include upvoted posts. Continuing with saved posts only.")
        else:
            log_error(f"Failed to fetch upvoted posts for user '{config.reddit_username}'. "
                      f"Status code: {response.status_code}")
        return None
    except Exception as e:
        log_error(f"Error fetching upvoted posts: {e}")
        return None


def extract_media_links(payload, origin="saved"):
    """Same URL-substring match as the script. Returns a list of dicts
    (url + post metadata + origin), deduped by URL preserving first-seen order."""
    media_links = []
    try:
        for post in payload['data']['children']:
            data = post['data']
            url = data.get('url', '')
            if any(ext in url for ext in MEDIA_MARKERS):
                media_links.append({
                    'url': url,
                    'post_title': data.get('title', ''),
                    'post_permalink': data.get('permalink', ''),
                    'subreddit': (data.get('subreddit') or ''),
                    'author': (data.get('author') or ''),
                    'source': origin,
                })
    except Exception as e:
        log_error(f"Error extracting media links: {e}")
    # Dedupe within this run, preserving order, so the same URL twice in one
    # batch is only sent once.
    seen = {}
    for item in media_links:
        seen.setdefault(item['url'], item)
    return list(seen.values())


def merge_sources(saved_items, upvoted_items):
    """Merge the two sources, saved first. A URL found in both keeps the
    first-seen metadata but is marked source='both'."""
    merged = {}
    for item in saved_items + upvoted_items:
        existing = merged.get(item['url'])
        if existing is None:
            merged[item['url']] = item
        elif existing.get('source') != item.get('source'):
            existing['source'] = 'both'
    return list(merged.values())


def sent_urls() -> set:
    """URLs JDownloader already accepted (the DB equivalent of sent_links.txt)."""
    session = get_session()
    try:
        return {u for (u,) in session.query(Link.url).filter(Link.status == 'sent').all()}
    finally:
        session.close()


def filter_new_links(items):
    """Drop items already sent. Failed sends stay eligible for retry."""
    already = sent_urls()
    return [item for item in items if item['url'] not in already]


def mark_links(items, status: str, run_id: int | None) -> None:
    """Record found links. 'sent' only ever happens after JDownloader accepted
    them; 'failed' rows stay retryable (filter_new_links only excludes sent)."""
    now = datetime.now()
    session = get_session()
    try:
        for item in items:
            source = item.get('source') or 'saved'
            row = session.query(Link).filter(Link.url == item['url']).first()
            if row:
                row.status = status
                row.run_id = run_id
                if item.get('author'):
                    row.author = item['author']
                # a URL previously seen from one source can show up in both
                if row.source and row.source != source:
                    row.source = 'both'
                if status == 'sent':
                    row.sent_at = now
                else:
                    row.found_at = row.found_at or now
            else:
                session.add(Link(
                    url=item['url'],
                    post_title=item.get('post_title', ''),
                    post_permalink=item.get('post_permalink', ''),
                    subreddit=item.get('subreddit', ''),
                    author=item.get('author', ''),
                    source=source,
                    found_at=now,
                    sent_at=now if status == 'sent' else None,
                    status=status,
                    run_id=run_id,
                ))
        session.commit()
    finally:
        session.close()


# --- JDownloader --------------------------------------------------------------
JD_PACKAGE_NAME = "RedditMedia"


def send_to_jdownloader(config: ScanConfig, urls) -> int:
    try:
        api = Myjdapi()
        api.connect(config.email, config.password)
        device = api.get_device(config.device_name)
        if device:
            response = device.linkgrabber.add_links([{
                "autostart": True,
                "links": "\n".join(urls),
                "packageName": JD_PACKAGE_NAME,
                "destinationFolder": config.destination_folder
            }])
            log_info(f"Links sent to JDownloader: {response}")
            # autostart in addLinks is unreliable: JDownloader parks the links
            # in LinkGrabber regardless. Move our package(s) to the download
            # list so they start downloading immediately.
            _start_grabbed_packages(device)
            return len(urls)  # Return the number of links sent
        else:
            log_error("JDownloader device not found.")
            return 0
    except Exception as e:
        log_error(f"Error sending links to JDownloader: {e}")
        return 0


def _start_grabbed_packages(device) -> None:
    """Move our packages from LinkGrabber into the download list.

    A failed move is only logged: the links were already accepted by
    JDownloader, so they still count as sent (retrying would duplicate them).
    """
    try:
        packages = device.linkgrabber.query_packages() or []
        ids = [p.get("uuid") for p in packages
               if p.get("name") == JD_PACKAGE_NAME and p.get("uuid")]
        if ids:
            device.linkgrabber.move_to_downloadlist([], ids)
            log_info(f"Moved {len(ids)} package(s) to the JDownloader download list.")
    except Exception as e:
        log_error(f"Could not auto-start JDownloader downloads: {e}")


# --- Pushover -----------------------------------------------------------------
def send_pushover_notification(message: str, config: ScanConfig | None = None,
                               kind: str = "results") -> None:
    if not notifications_enabled(kind):
        log_info(f"Pushover suppressed by settings (would have sent: {message[:60]}...)")
        return
    cfg = config or ScanConfig.from_env()
    if cfg is None:
        log_error("Cannot send Pushover notification: missing credentials.")
        return
    try:
        url = "https://api.pushover.net/1/messages.json"
        payload = {
            "token": cfg.pushover_api_token,
            "user": cfg.pushover_user_key,
            "message": message,
            "title": "RedSaves Notification"
        }
        response = requests.post(url, data=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            log_info("Pushover notification sent successfully.")
        else:
            log_error(f"Failed to send Pushover notification. Status code: {response.status_code}")
    except Exception as e:
        log_error(f"Error sending Pushover notification: {e}")


# --- Main scan ----------------------------------------------------------------
def run_scan(config: ScanConfig | None = None) -> int | None:
    """Execute one full scan. Returns the runs.id, or None if a scan is already
    running. Never raises for missing credentials (records an auth_failed run)."""
    if not _scan_lock.acquire(blocking=False):
        log_info("Scan skipped: another scan is already running.")
        return None
    try:
        return _run_scan(config or ScanConfig.from_env())
    finally:
        _scan_lock.release()


def _run_scan(config: ScanConfig | None) -> int:
    rotate_log_if_needed()
    session = get_session()
    run = Run(started_at=datetime.now(), status="running")
    session.add(run)
    session.commit()
    run_id = run.id
    session.close()

    def finish(status: str, found=0, new=0, sent=0, error=""):
        session = get_session()
        try:
            row = session.query(Run).filter(Run.id == run_id).first()
            row.finished_at = datetime.now()
            row.status = status
            row.links_found = found
            row.links_new = new
            row.links_sent = sent
            row.error = error
            session.commit()
        finally:
            session.close()
        return run_id

    if config is None:
        msg = "Missing required environment variables in .env file."
        log_error(msg)
        return finish("auth_failed", error=msg)

    token = get_access_token(config)
    if token:
        reset_auth_failures()
        limit = get_post_limit()
        saved_posts = fetch_saved_posts(config, token, limit)
        if saved_posts:
            saved_items = extract_media_links(saved_posts, origin="saved")
            if include_upvoted():
                upvoted_posts = fetch_upvoted_posts(config, token, limit)
                upvoted_items = (extract_media_links(upvoted_posts, origin="upvoted")
                                 if upvoted_posts else [])
                media_links = merge_sources(saved_items, upvoted_items)
            else:
                media_links = saved_items
            if media_links:
                new_links = filter_new_links(media_links)
                if new_links:
                    log_info(f"Found {len(new_links)} new media links.")
                    count = send_to_jdownloader(config, [i['url'] for i in new_links])
                    if count > 0:
                        # Only mark links as sent when JDownloader accepted them
                        mark_links(new_links, 'sent', run_id)
                        send_pushover_notification(
                            f"{count} new media links have been sent to JDownloader!",
                            config)
                        return finish("ok", found=len(media_links),
                                      new=len(new_links), sent=count)
                    else:
                        mark_links(new_links, 'failed', run_id)
                        log_error("JDownloader send failed; links kept out of history "
                                  "for retry next run.")
                        send_pushover_notification(
                            "Failed to send media links to JDownloader.", config)
                        return finish("send_failed", found=len(media_links),
                                      new=len(new_links), sent=0,
                                      error="JDownloader did not accept the links")
                else:
                    log_info("No new media links to send.")
                    send_pushover_notification("No new media links found.", config)
                    return finish("no_new", found=len(media_links))
            else:
                log_info("No media links found.")
                send_pushover_notification("No media links found in saved posts.", config)
                return finish("no_new")
        else:
            log_info("No saved posts fetched.")
            send_pushover_notification("Failed to fetch saved posts.", config)
            return finish("fetch_failed", error="Reddit saved-posts fetch failed")
    else:
        # Reddit auth failed: count it; the watchdog alerts if this keeps happening
        record_auth_failure(lambda msg: send_pushover_notification(msg, config, kind="watchdog"))
        return finish("auth_failed", error="Reddit access token request failed")
