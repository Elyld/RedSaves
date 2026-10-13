"""RedSaves scan logic, DB-backed refactor of RedSaves2.0.py (v2.4).

Behavior is deliberately identical to the standalone script:
- first 25 saved posts only, no pagination
- media match: '.gif', '.mp4', 'redgifs', 'v.redd.it' as URL substrings
- within-run URL dedup preserving order
- links are marked sent ONLY when JDownloader accepts them (a failed send
  burns nothing; the links are retried on the next run)
- 15s timeouts everywhere, rotating 1 MB log file
- Reddit auth watchdog: consecutive failures counted, Pushover alert every 3rd
- Pushover messages verbatim, including the 'no new links' pings
- same env var names as the script

What changed vs the script: sent_links.txt / auth_failures.txt file I/O is
now SQLite (links / settings tables); every scan writes a runs row; post
title/permalink/subreddit are captured alongside each URL for the history UI.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from myjdapi import Myjdapi

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

MEDIA_MARKERS = ('.gif', '.mp4', 'redgifs', 'v.redd.it')

REQUIRED_ENV = ('EMAIL', 'PASSWORD', 'DEVICE_NAME', 'CLIENT_ID', 'SECRET_TOKEN',
                'REFRESH_TOKEN', 'REDDIT_USERNAME', 'DESTINATION_FOLDER',
                'PUSHOVER_USER_KEY', 'PUSHOVER_API_TOKEN')

_scan_lock = threading.Lock()


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


def fetch_saved_posts(config: ScanConfig, token):
    try:
        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
        response = requests.get(
            f'https://oauth.reddit.com/user/{config.reddit_username}/saved',
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


def extract_media_links(saved_posts):
    """Same URL-substring match as the script. Returns a list of dicts
    (url + post metadata), deduped by URL preserving first-seen order."""
    media_links = []
    try:
        for post in saved_posts['data']['children']:
            data = post['data']
            url = data.get('url', '')
            if any(ext in url for ext in MEDIA_MARKERS):
                media_links.append({
                    'url': url,
                    'post_title': data.get('title', ''),
                    'post_permalink': data.get('permalink', ''),
                    'subreddit': (data.get('subreddit') or ''),
                    'author': (data.get('author') or ''),
                })
    except Exception as e:
        log_error(f"Error extracting media links: {e}")
    # Dedupe within this run, preserving order, so the same URL twice in one
    # batch is only sent once.
    seen = {}
    for item in media_links:
        seen.setdefault(item['url'], item)
    return list(seen.values())


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
            row = session.query(Link).filter(Link.url == item['url']).first()
            if row:
                row.status = status
                row.run_id = run_id
                if item.get('author'):
                    row.author = item['author']
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
        saved_posts = fetch_saved_posts(config, token)
        if saved_posts:
            media_links = extract_media_links(saved_posts)
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
