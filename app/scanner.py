"""RedSaves scan logic, DB-backed refactor of RedSaves2.0.py (v2.4).

Behavior is deliberately identical to the standalone script, except where
the WebUI settings say otherwise:
- saved posts fetched with ?limit= (post_limit setting, default 25, max 100)
- media match: '.gif', '.mp4', '.jpg', '.jpeg', '.png', '.webp',
  'redgifs', 'v.redd.it' as URL substrings
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

MEDIA_MARKERS = ('.gif', '.mp4', '.jpg', '.jpeg', '.png', '.webp',
                 'redgifs', 'v.redd.it')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')

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


# Last HTTP status seen from /api/info (None = no request yet). Lets the
# runner abort phase 1 early when Reddit refuses the endpoint outright.
_last_info_status = None


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


def _post_thumbnail(data):
    """Best thumbnail URL from a Reddit post data dict, or ''. Prefers the
    high-res preview image; skips Reddit's placeholder strings."""
    preview = (data.get('preview') or {}).get('images') or []
    if preview:
        src = (preview[0].get('source') or {}).get('url') or ''
        if src:
            return src.replace('&amp;', '&')
    thumb = data.get('thumbnail') or ''
    if thumb in ('', 'self', 'default', 'nsfw', 'spoiler'):
        return ''
    return thumb.replace('&amp;', '&')


def _post_duration(data):
    """Video duration in seconds from reddit_video metadata, or None."""
    try:
        return int((data.get('media') or {}).get('reddit_video', {}).get('duration'))
    except (TypeError, ValueError, AttributeError):
        return None


def is_picture(url="", local_file=""):
    """True when the media is a still image (not a video).

    Checks the local filename first (a downloaded .jpg is a picture even
    though it plays from /media-file), then the source URL.
    """
    target = (local_file or url or "").lower()
    return target.endswith(IMAGE_EXTS)


def media_embed(url, link_id=None, local_file=""):
    """How to play a stored URL in the media center. Returns (kind, src):

    - ('video', '/media-file/{id}'): downloaded file on local disk — preferred,
      bypasses regional blocks entirely
    - ('image', '/media-file/{id}'): downloaded picture on local disk
    - ('redgifs', ifr_url): embeddable redgifs player for watch/ifr/v3 forms
    - ('video', url): direct mp4 (v.redd.it media files, imgur mp4)
    - ('image', url): direct image (i.redd.it / imgur jpg, png, webp, gif)
    - ('link', url): anything else — open the original in a new tab
    """
    if local_file and link_id is not None:
        if is_picture(local_file=local_file):
            return ("image", f"/media-file/{link_id}")
        return ("video", f"/media-file/{link_id}")
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        path = p.path or ""
        segs = [s for s in path.split("/") if s]
        if host in ("redgifs.com", "www.redgifs.com", "v3.redgifs.com"):
            if len(segs) >= 2 and segs[0] in ("watch", "ifr"):
                return ("redgifs", f"https://www.redgifs.com/ifr/{segs[1]}")
            return ("link", url)
        if host == "v.redd.it":
            return ("video", url)
        low = path.lower()
        if low.endswith(IMAGE_EXTS):
            return ("image", url)
        if low.endswith(".mp4"):
            return ("video", url)
        return ("link", url)
    except Exception:
        return ("link", url)


def _canonicalize_url(url):
    """Normalize a saved media URL for dupe matching and the /api/info lookup.

    Unifies scheme (https) and strips www. so www-vs-bare and http-vs-https
    variants of the same file group together; redgifs hosts collapse to
    redgifs.com with /ifr/ rewritten to /watch/; v.redd.it collapses to the
    post id. The stored URL is never rewritten.
    """
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
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
        # default: unified scheme, bare host, drop query/fragment
        return f"https://{host}{path}"
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
    global _last_info_status
    try:
        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
        for attempt in _url_variants(url):
            _throttle()  # every request, not just every link: variants would
            # otherwise burst 5x faster than Reddit's rate limit allows
            response = requests.get('https://oauth.reddit.com/api/info',
                                    params={'url': attempt},
                                    headers=headers, timeout=REQUEST_TIMEOUT)
            _last_info_status = response.status_code
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
                    'score': data.get('score') or 0,
                    'num_comments': data.get('num_comments') or 0,
                    'thumbnail': _post_thumbnail(data),
                    'duration': _post_duration(data),
                    'domain': (data.get('domain') or ''),
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
        if not row.thumbnail and meta.get('thumbnail'):
            row.thumbnail = meta['thumbnail']
        if not row.duration and meta.get('duration'):
            row.duration = meta['duration']
        if not row.domain and meta.get('domain'):
            row.domain = meta['domain']
        if not row.flair and meta.get('flair'):
            row.flair = meta['flair']
        ratio = _as_float(meta.get('upvote_ratio'))
        if ratio is not None:
            row.upvote_ratio = ratio
        # score/comments are volatile: always take the fresh reading
        row.score = meta.get('score') or 0
        row.num_comments = meta.get('num_comments') or 0
        if title_was_empty and meta.get('created_utc'):
            try:
                row.sent_at = datetime.fromtimestamp(float(meta['created_utc']))
            except (ValueError, TypeError, OSError):
                pass
        session.commit()
    finally:
        session.close()


def _as_float(value):
    """float(value) or None — Reddit sometimes sends odd values."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def refresh_metadata(link_id: int, meta: dict) -> None:
    """Backfill pass for links that already have titles: refresh volatile
    stats and fill empty media-data fields. Never touches the title,
    permalink, or sent_at."""
    session = get_session()
    try:
        row = session.query(Link).filter(Link.id == link_id).first()
        if not row:
            return
        if meta.get('thumbnail'):
            row.thumbnail = meta['thumbnail']
        if meta.get('duration'):
            row.duration = meta['duration']
        if not row.domain and meta.get('domain'):
            row.domain = meta['domain']
        if not row.author and meta.get('author'):
            row.author = meta['author']
        if not row.subreddit and meta.get('subreddit'):
            row.subreddit = meta['subreddit']
        if not row.post_permalink and meta.get('post_permalink'):
            row.post_permalink = meta['post_permalink']
        row.score = meta.get('score') or 0
        row.num_comments = meta.get('num_comments') or 0
        if not row.flair and meta.get('flair'):
            row.flair = meta['flair']
        ratio = _as_float(meta.get('upvote_ratio'))
        if ratio is not None:
            row.upvote_ratio = ratio
        session.commit()
    finally:
        session.close()


def _iter_all_saved_posts(config: ScanConfig, token):
    """Yield raw saved-post data dicts, paging through the *entire* saved
    list via the `after` cursor (100 per page). Stops on error."""
    headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
    after = None
    pages = 0
    while True:
        params = {'limit': 100}
        if after:
            params['after'] = after
        try:
            response = requests.get(
                f'https://oauth.reddit.com/user/{config.reddit_username}/saved',
                params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        except Exception as e:
            log_error(f"Saved-scan: error fetching page {pages + 1}: {e}")
            return
        if response.status_code != 200:
            log_error(f"Saved-scan: HTTP {response.status_code} on page {pages + 1}")
            return
        data = (response.json() or {}).get('data') or {}
        children = data.get('children') or []
        pages += 1
        for child in children:
            yield (child or {}).get('data') or {}
        after = data.get('after')
        if not after or not children:
            break
        _throttle()
    log_info(f"Saved-scan: paged through {pages} saved page(s).")


def _all_links() -> list:
    """(id, url) pairs for every link, oldest first."""
    session = get_session()
    try:
        rows = (session.query(Link.id, Link.url)
                .order_by(Link.id).all())
        return [(r.id, r.url) for r in rows]
    finally:
        session.close()


def _run_deep_match(config: ScanConfig, token, refresh: bool = False) -> int:
    """Phase 2 of enrichment: match links against the full saved-posts list
    by canonical URL. Default mode only touches still-title-less links;
    refresh=True updates media-data (thumbnail/score/…) on every matched
    link without touching titles. Never adds new links. Returns matches."""
    remaining = _all_links() if refresh else titleless_links()
    want = {}
    for link_id, url in remaining:
        want.setdefault(_canonicalize_url(url), link_id)
    if not want:
        return 0
    matched = 0
    scanned = 0
    for post in _iter_all_saved_posts(config, token):
        scanned += 1
        url = post.get('url', '') or ''
        if not any(ext in url for ext in MEDIA_MARKERS):
            continue
        link_id = want.get(_canonicalize_url(url))
        if link_id is None:
            continue
        meta = {
            'post_title': post.get('title', ''),
            'post_permalink': post.get('permalink', ''),
            'subreddit': (post.get('subreddit') or ''),
            'author': (post.get('author') or ''),
            'created_utc': post.get('created_utc'),
            'score': post.get('score') or 0,
            'num_comments': post.get('num_comments') or 0,
            'thumbnail': _post_thumbnail(post),
            'duration': _post_duration(post),
            'domain': (post.get('domain') or ''),
            'flair': (post.get('link_flair_text') or ''),
            'upvote_ratio': post.get('upvote_ratio'),
        }
        if refresh:
            refresh_metadata(link_id, meta)
        else:
            apply_enrichment(link_id, meta)
            want.pop(_canonicalize_url(url), None)
        matched += 1
        _enrich_progress['found'] += 1
        _enrich_progress['processed'] += 1
        if not refresh and not want:
            break
    if not refresh:
        _enrich_progress['skipped'] = len(want)
    log_info(f"Saved-scan ({'refresh' if refresh else 'enrich'}): matched "
             f"{matched} link(s) from {scanned} saved post(s).")
    return matched


def run_metadata_refresh(config: ScanConfig | None = None) -> bool:
    """One-shot backfill of thumbnails/stats for already-titled links, via the
    saved-posts scan. Returns False when another enrich/refresh is running."""
    if not _enrich_lock.acquire(blocking=False):
        log_info("Metadata refresh skipped: another enrichment is already running.")
        return False
    try:
        _run_metadata_refresh(config or ScanConfig.from_env())
    finally:
        _enrich_lock.release()
    return True


def _run_metadata_refresh(config: ScanConfig | None) -> None:
    global _enrich_progress
    _enrich_progress = {"running": True, "phase": "refresh", "processed": 0,
                        "total": 0, "found": 0, "skipped": 0}
    try:
        set_enrich_note("")
        if config is None:
            set_enrich_note("Metadata refresh couldn't start — Reddit credentials are missing.")
            return
        token = get_access_token(config)
        if not token:
            set_enrich_note("Metadata refresh couldn't get a Reddit token — see the Logs page.")
            return
        total = len(_all_links())
        _enrich_progress["total"] = total
        log_info(f"Metadata refresh started: {total} link(s).")
        _run_deep_match(config, token, refresh=True)
        log_info(f"Metadata refresh finished: {_enrich_progress['found']} matched.")
    finally:
        _enrich_progress["running"] = False


# --- Dead-link checker ---------------------------------------------------------

_linkcheck_lock = threading.Lock()
_linkcheck_progress = {"running": False, "processed": 0, "total": 0, "dead": 0}

RG_TEMP_AUTH_URL = "https://api.redgifs.com/v2/auth/temporary"


def get_linkcheck_progress() -> dict:
    return dict(_linkcheck_progress)


_rg_token = None


def _redgifs_token():
    """Temporary redgifs API token (no user auth needed). Cached in memory."""
    global _rg_token
    if _rg_token:
        return _rg_token
    try:
        r = requests.get(RG_TEMP_AUTH_URL, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            _rg_token = (r.json() or {}).get('token')
    except Exception as e:
        log_error(f"redgifs temp auth failed: {e}")
    return _rg_token


def _redgifs_lookup(slug):
    """(alive, size, creator, details) via the redgifs API. alive=None when
    unknown (API hiccup) — the row is left alone rather than false-flagged.
    details carries whatever else the API returned: tags, views, likes."""
    global _rg_token
    token = _redgifs_token()
    if not token:
        return None, None, None, {}
    try:
        r = requests.get(f'https://api.redgifs.com/v2/gifs/{slug}',
                         headers={'Authorization': f'Bearer {token}'},
                         timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            gif = (r.json() or {}).get('gif') or {}
            details = {
                "tags": [t for t in (gif.get('tags') or []) if t],
                "views": gif.get('views'),
                "likes": gif.get('likes'),
            }
            return True, None, (gif.get('userName') or ''), details
        if r.status_code == 404:
            return False, None, None, {}
        if r.status_code == 401:
            _rg_token = None  # expired; next link re-auths
    except Exception as e:
        log_error(f"redgifs lookup failed for {slug[:40]}: {e}")
    return None, None, None, {}


def _check_url_alive(url):
    """Returns (alive, size, creator, details); alive=None means 'couldn't
    tell'. details is redgifs tags/views/likes, else {}."""
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        segs = [s for s in (p.path or '').split('/') if s]
        if (host in ('redgifs.com', 'www.redgifs.com', 'v3.redgifs.com')
                and len(segs) >= 2 and segs[0] in ('watch', 'ifr')):
            return _redgifs_lookup(segs[1])
        r = requests.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True,
                          headers={'User-Agent': 'RedSaves v2.4'})
        if r.status_code == 405:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, stream=True,
                             headers={'User-Agent': 'RedSaves v2.4',
                                      'Range': 'bytes=0-0'})
        alive = r.status_code < 400
        size = r.headers.get('Content-Length')
        return alive, (int(size) if size and size.isdigit() else None), None, {}
    except Exception:
        return None, None, None, {}


def run_link_check() -> bool:
    """Background dead-link check over every stored link. Returns False when
    one is already running."""
    if not _linkcheck_lock.acquire(blocking=False):
        log_info("Link check skipped: another check is already running.")
        return False
    try:
        _run_link_check()
    finally:
        _linkcheck_lock.release()
    return True


def _run_link_check() -> None:
    global _linkcheck_progress
    links = _all_links()
    _linkcheck_progress = {"running": True, "processed": 0,
                           "total": len(links), "dead": 0}
    log_info(f"Link check started: {len(links)} link(s).")
    session = get_session()
    try:
        try:
            for link_id, url in links:
                alive, size, creator, details = _check_url_alive(url)
                row = session.query(Link).filter(Link.id == link_id).first()
                if row is not None and alive is not None:
                    row.alive = alive
                    row.checked_at = datetime.now()
                    if size is not None:
                        row.file_size = size
                    if creator:
                        row.creator = creator
                    tags = details.get("tags") or []
                    if tags:
                        row.tags = ", ".join(tags[:20])
                    if details.get("views") is not None:
                        try:
                            row.views = int(details["views"])
                        except (TypeError, ValueError):
                            pass
                    if details.get("likes") is not None:
                        try:
                            row.likes = int(details["likes"])
                        except (TypeError, ValueError):
                            pass
                    if not alive:
                        _linkcheck_progress["dead"] += 1
                _linkcheck_progress["processed"] += 1
                session.commit()
                time.sleep(1.0)  # polite: ~1 req/s across mixed hosts
        finally:
            session.close()
    finally:
        _linkcheck_progress["running"] = False
    log_info(f"Link check finished: {_linkcheck_progress['dead']} dead "
             f"of {len(links)} checked.")


# --- Local file index ----------------------------------------------------------

def media_dir() -> Path:
    """Where JDownloader downloads live, as seen from this container.
    Mount the downloads folder here and it's the only thing to configure."""
    return Path(os.environ.get("MEDIA_DIR", "/downloads"))


MEDIA_EXTS = {".mp4", ".webm", ".gif", ".mov", ".mkv",
              ".jpg", ".jpeg", ".png", ".webp"}


def media_keys(url) -> list:
    """Match keys for finding a link's downloaded file: the redgifs slug,
    the v.redd.it id, or the image id — whatever JDownloader likely kept in
    the filename."""
    try:
        p = urlsplit(url)
        segs = [s for s in (p.path or "").split("/") if s]
        host = (p.hostname or "").lower()
        if (host in ("redgifs.com", "www.redgifs.com", "v3.redgifs.com")
                and len(segs) >= 2 and segs[0] in ("watch", "ifr")):
            return [segs[1].lower()]
        if host == "v.redd.it" and segs:
            return [segs[0].lower()]
        if segs:
            stem = segs[-1].rsplit(".", 1)[0].lower()
            return [stem] if stem else []
        return []
    except Exception:
        return []


_filescan_lock = threading.Lock()
_filescan_progress = {"running": False, "processed": 0, "total": 0, "matched": 0}


def get_filescan_progress() -> dict:
    return dict(_filescan_progress)


def run_filescan() -> bool:
    """Index downloaded video files and match them to links. Background."""
    if not _filescan_lock.acquire(blocking=False):
        log_info("Local file scan skipped: another scan is already running.")
        return False
    try:
        _run_filescan()
    finally:
        _filescan_lock.release()
    return True


def _run_filescan() -> None:
    global _filescan_progress
    root = media_dir()
    _filescan_progress = {"running": True, "processed": 0, "total": 0, "matched": 0}
    try:
        if not root.is_dir():
            log_error(f"Local file scan: MEDIA_DIR {root} is not a directory — "
                      f"mount the downloads folder there.")
            return
        indexed = []
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if Path(fn).suffix.lower() not in MEDIA_EXTS:
                    continue
                full = Path(dirpath) / fn
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                if size > 0:
                    indexed.append((fn.rsplit(".", 1)[0].lower(), full, size))
        log_info(f"Local file scan: {len(indexed)} media file(s) under {root}.")
        links = _all_links()
        _filescan_progress["total"] = len(links)
        session = get_session()
        try:
            for link_id, url in links:
                keys = [k for k in media_keys(url) if k]
                best = None
                best_size = -1
                for stem, full, size in indexed:
                    if any(k in stem for k in keys):
                        if size > best_size:
                            best, best_size = full, size
                row = session.query(Link).filter(Link.id == link_id).first()
                if row is not None and best is not None:
                    try:
                        row.local_file = str(best.relative_to(root))
                    except ValueError:
                        row.local_file = str(best)
                    _filescan_progress["matched"] += 1
                _filescan_progress["processed"] += 1
                session.commit()
        finally:
            session.close()
    finally:
        _filescan_progress["running"] = False
    log_info(f"Local file scan finished: {_filescan_progress['matched']} matched.")


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
    _enrich_progress = {"running": True, "phase": "lookup", "processed": 0,
                        "total": 0, "found": 0, "skipped": 0}
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
        info_403s = 0
        for link_id, url in targets:
            meta = fetch_info_by_url(config, token, url)
            if _last_info_status == 403:
                # Systematic refusal, not a per-URL miss: stop burning
                # requests and fall through to the saved-scan phase.
                info_403s += 1
                _enrich_progress["skipped"] += 1
                _enrich_progress["processed"] += 1
                if info_403s >= 3:
                    log_error("Enrich phase 1 aborted: Reddit refused /api/info "
                              "(HTTP 403) — the token likely lacks the needed "
                              "scope. Continuing with the saved-posts scan, "
                              "which uses the working /saved endpoint.")
                    set_enrich_note("Reddit refused the title-lookup requests "
                                    "(HTTP 403 — probably the Reddit token's scope). "
                                    "The saved-posts scan below doesn't need it.")
                    break
                continue
            info_403s = 0
            if meta:
                apply_enrichment(link_id, meta)
                _enrich_progress["found"] += 1
            else:
                _enrich_progress["skipped"] += 1
            _enrich_progress["processed"] += 1
            # (no extra throttle here: fetch_info_by_url already throttles
            # before every request, which spaces links apart too)
        # Phase 2: sweep the full saved list for anything the lookup missed.
        _enrich_progress["phase"] = "saved-scan"
        still_missing = titleless_count()
        _enrich_progress["total"] = still_missing
        _enrich_progress["processed"] = 0
        if still_missing:
            log_info(f"Enrich phase 2: scanning saved posts for "
                     f"{still_missing} unmatched link(s).")
            _run_deep_match(config, token)
        log_info(f"Enrich finished: {_enrich_progress['found']} enriched, "
                 f"{_enrich_progress['skipped']} still missing metadata.")
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
                    # free metadata riding along in the same response
                    'score': data.get('score') or 0,
                    'num_comments': data.get('num_comments') or 0,
                    'thumbnail': _post_thumbnail(data),
                    'duration': _post_duration(data),
                    'domain': (data.get('domain') or ''),
                    'created_utc': data.get('created_utc'),
                    'flair': (data.get('link_flair_text') or ''),
                    'upvote_ratio': data.get('upvote_ratio'),
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
                # refresh volatile metadata on rows we see again
                row.score = item.get('score') or 0
                row.num_comments = item.get('num_comments') or 0
                if item.get('thumbnail'):
                    row.thumbnail = item['thumbnail']
                if item.get('duration'):
                    row.duration = item['duration']
                if item.get('domain'):
                    row.domain = item['domain']
                if not row.flair and item.get('flair'):
                    row.flair = item['flair']
                ratio = _as_float(item.get('upvote_ratio'))
                if ratio is not None:
                    row.upvote_ratio = ratio
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
                    score=item.get('score') or 0,
                    num_comments=item.get('num_comments') or 0,
                    thumbnail=item.get('thumbnail') or '',
                    duration=item.get('duration'),
                    domain=item.get('domain') or '',
                    flair=item.get('flair') or '',
                    upvote_ratio=_as_float(item.get('upvote_ratio')),
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


# --- JDownloader status sync ----------------------------------------------------

_jdsync_lock = threading.Lock()
_jdsync_progress = {"running": False, "processed": 0, "total": 0,
                    "done": 0, "failed": 0}


def get_jdsync_progress() -> dict:
    return dict(_jdsync_progress)


def _jd_link_state(jd_link: dict) -> str:
    """Map a myjdapi link dict to done|downloading|queued|failed."""
    if jd_link.get('finished'):
        return 'done'
    status = str(jd_link.get('status') or '').lower()
    if any(w in status for w in ('fail', 'error', 'offline', 'dead',
                                 'missing', 'skipped')):
        return 'failed'
    if (jd_link.get('bytesLoaded') or 0) > 0:
        return 'downloading'
    return 'queued'


def _jd_state_map(device) -> dict:
    """url -> state for every link JDownloader knows about (download list +
    linkgrabber). Collisions keep the most attention-worthy state."""
    rank = {'failed': 3, 'downloading': 2, 'queued': 1, 'done': 0}
    out = {}
    try:
        dl_links = device.downloads.query_links() or []
    except Exception as e:
        log_error(f"JD sync: downloads query failed: {e}")
        dl_links = []
    try:
        grab_links = device.linkgrabber.query_links() or []
    except Exception as e:
        log_error(f"JD sync: linkgrabber query failed: {e}")
        grab_links = []
    for jd_link in dl_links + grab_links:
        url = jd_link.get('url') or ''
        if not url:
            continue
        state = _jd_link_state(jd_link)
        if rank.get(state, 0) >= rank.get(out.get(url), -1):
            out[url] = state
    return out


def run_jd_sync() -> bool:
    """Match every stored link against JDownloader's lists, in the background.
    Returns False when one is already running."""
    if not _jdsync_lock.acquire(blocking=False):
        log_info("JD sync skipped: another sync is already running.")
        return False
    try:
        _run_jd_sync()
    finally:
        _jdsync_lock.release()
    return True


def _run_jd_sync() -> None:
    global _jdsync_progress
    _jdsync_progress = {"running": True, "processed": 0, "total": 0,
                        "done": 0, "failed": 0}
    config = ScanConfig.from_env()
    if config is None:
        log_error("JD sync: JDownloader credentials missing.")
        _jdsync_progress["running"] = False
        return
    try:
        api = Myjdapi()
        api.connect(config.email, config.password)
        device = api.get_device(config.device_name)
    except Exception as e:
        log_error(f"JD sync: could not reach JDownloader: {e}")
        _jdsync_progress["running"] = False
        return
    if not device:
        log_error("JD sync: JDownloader device not found.")
        _jdsync_progress["running"] = False
        return
    jd_urls = _jd_state_map(device)
    links = _all_links()
    _jdsync_progress["total"] = len(links)
    # Key fallback: JD often stores the resolved direct URL while we stored
    # the watch page (or vice versa) — match on redgifs slug / v.redd.it id /
    # filename stem when the exact URL isn't there.
    key_map = {}
    for url, state in jd_urls.items():
        for k in media_keys(url):
            key_map.setdefault(k, state)
    session = get_session()
    try:
        try:
            for link_id, url in links:
                state = jd_urls.get(url)
                if state is None:
                    for k in media_keys(url):
                        state = key_map.get(k)
                        if state:
                            break
                row = session.query(Link).filter(Link.id == link_id).first()
                if row is not None and state:
                    row.jd_status = state
                    if state == 'done':
                        _jdsync_progress["done"] += 1
                    elif state == 'failed':
                        _jdsync_progress["failed"] += 1
                _jdsync_progress["processed"] += 1
                session.commit()
        finally:
            session.close()
    finally:
        _jdsync_progress["running"] = False
    log_info(f"JD sync finished: {_jdsync_progress['done']} done, "
             f"{_jdsync_progress['failed']} failed of {len(links)} link(s).")
    try:
        api.disconnect()
    except Exception:
        pass


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


def _prune_old_runs(session, keep=200) -> int:
    """Delete runs beyond the newest `keep` — the table grows 2 rows/day."""
    old = session.query(Run).order_by(Run.id.desc()).offset(keep).all()
    for row in old:
        session.delete(row)
    return len(old)


def _run_scan(config: ScanConfig | None) -> int:
    rotate_log_if_needed()
    session = get_session()
    run = Run(started_at=datetime.now(), status="running")
    session.add(run)
    session.commit()
    run_id = run.id
    _prune_old_runs(session)
    session.commit()
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


# --- Dupe detector --------------------------------------------------------------

_POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)/", re.IGNORECASE)


def _post_id_from_permalink(permalink: str) -> str:
    m = _POST_ID_RE.search(permalink or "")
    return m.group(1).lower() if m else ""


def _dupe_winner(members) -> object:
    """Pick the link to keep: favorite first, then local file, then the one
    with the most metadata, then the oldest row."""
    def rank(link):
        return (
            bool(link.favorite),
            bool(link.local_file),
            bool(link.post_title),
            bool(link.thumbnail),
            (link.score or 0),
        )
    best = max(members, key=rank)
    # max() keeps the first on ties — order members oldest-first for stability
    return best


def find_dupe_groups() -> list:
    """Groups of links that look like the same post saved twice: same Reddit
    post id (redgifs + v.redd.it versions of one post) or same canonical URL
    (www vs bare host, http vs https). Returns a list of dicts:
    {'reason': ..., 'links': [Link, ...], 'winner_id': ...}."""
    session = get_session()
    try:
        links = session.query(Link).order_by(Link.id).all()
        session.expunge_all()
    finally:
        session.close()
    groups = []
    seen_ids = set()

    by_post = {}
    for link in links:
        pid = _post_id_from_permalink(link.post_permalink or "")
        if pid:
            by_post.setdefault(pid, []).append(link)
    for pid, members in sorted(by_post.items()):
        if len(members) > 1:
            groups.append({"reason": "same post", "key": f"post:{pid}",
                           "links": members})
            seen_ids.update(l.id for l in members)

    by_url = {}
    for link in links:
        if link.id in seen_ids:
            continue
        by_url.setdefault(_canonicalize_url(link.url), []).append(link)
    for curl, members in sorted(by_url.items()):
        if len(members) > 1:
            groups.append({"reason": "same file", "key": f"url:{curl}",
                           "links": members})

    for g in groups:
        g["winner_id"] = _dupe_winner(g["links"]).id
    return groups


_dupe_cache = {"at": 0.0, "groups": []}
DUPE_CACHE_TTL = 300  # seconds; the /media badge doesn't need fresh data


def find_dupe_groups_cached() -> list:
    """find_dupe_groups() with a short TTL — the /media badge was paying for
    a full-table scan on every page load."""
    now = time.monotonic()
    if now - _dupe_cache["at"] >= DUPE_CACHE_TTL:
        _dupe_cache["groups"] = find_dupe_groups()
        _dupe_cache["at"] = now
    return _dupe_cache["groups"]


def invalidate_dupe_cache() -> None:
    _dupe_cache["at"] = 0.0


def merge_dupe_group(winner_id: int, loser_ids: list) -> int:
    """Fold loser metadata into the winner, then delete the losers.

    A dupe row can hold the only pointer to a downloaded file (or the only
    ★), so the winner inherits anything it lacks before anything is deleted
    — merging never loses a file pointer, a favorite, or the best stats."""
    losers = [i for i in loser_ids if i != winner_id]
    if not losers:
        return 0
    session = get_session()
    try:
        winner = session.query(Link).filter(Link.id == winner_id).first()
        loser_rows = session.query(Link).filter(Link.id.in_(losers)).all()
        if winner is not None:
            for loser in loser_rows:
                for field in ('local_file', 'thumbnail', 'tags', 'creator',
                              'flair', 'post_title', 'post_permalink', 'domain',
                              'author', 'subreddit', 'jd_status'):
                    if not getattr(winner, field, None) and getattr(loser, field, None):
                        setattr(winner, field, getattr(loser, field))
                for field in ('score', 'num_comments', 'views', 'likes',
                              'file_size'):
                    wv, lv = getattr(winner, field, None), getattr(loser, field, None)
                    if lv and (not wv or lv > wv):
                        setattr(winner, field, lv)
                for field in ('duration', 'upvote_ratio', 'checked_at'):
                    if (getattr(winner, field, None) is None
                            and getattr(loser, field, None) is not None):
                        setattr(winner, field, getattr(loser, field))
                if not winner.alive and loser.alive:
                    winner.alive = loser.alive
                if loser.favorite:
                    winner.favorite = True
        deleted = (session.query(Link)
                   .filter(Link.id.in_(losers))
                   .delete(synchronize_session=False))
        session.commit()
        invalidate_dupe_cache()
        return deleted
    finally:
        session.close()


def purge_graveyard() -> dict:
    """Delete dead links — but never a row backed by a file we actually have.
    A dead URL with a matched local file or jd_status='done' is skipped and
    reported, so purging can't orphan downloads. Returns
    {'deleted': n, 'skipped': n}."""
    session = get_session()
    try:
        dead = session.query(Link).filter(Link.alive.is_(False)).all()
        protected = [l.id for l in dead
                     if (l.local_file or '') or (l.jd_status or '') == 'done']
        doomed = [l.id for l in dead if l.id not in protected]
        if doomed:
            session.query(Link).filter(Link.id.in_(doomed)).delete(
                synchronize_session=False)
            session.commit()
        log_info(f"Graveyard purge: deleted {len(doomed)}, "
                 f"skipped {len(protected)} (file on disk / downloaded).")
        return {"deleted": len(doomed), "skipped": len(protected)}
    finally:
        session.close()
