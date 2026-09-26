# RedSaves 2.4
# Rework notes (2026-09-24/25):
# - Added request timeouts so a hung Reddit/JDownloader/Pushover call fails fast
#   instead of blocking the run forever.
# - sent_links.txt now resolves relative to this script's folder, so it lands in
#   the right place even if the working directory differs (e.g. under cron).
# - REDDIT_USERNAME added to the required-env check (was checked later instead).
# - History file is only updated when JDownloader actually accepted the links;
#   a failed send no longer burns the links or silently drops them.
# - v.redd.it (Reddit-hosted video) links are now picked up alongside .gif,
#   .mp4, and redgifs.
# - Links are deduped within each run, so the same URL twice in one batch is
#   only sent once.
# - v2.4: run log (redsaves.log, rotates at 1 MB) captures errors and key run
#   events, so the scheduler's console output is no longer the only record.
# - v2.4: Reddit auth watchdog — consecutive token failures are counted in
#   auth_failures.txt and trigger a Pushover alert every 3rd failure, so an
#   expired refresh token gets noticed instead of silently downloading nothing.
# Deliberately unchanged: no pagination (first 25 saved posts only),
# notification messages/frequency, env var names, JDownloader package layout,
# and overall flow.
#
# Known limitation: dedup is URL-based. If the SAME video is saved as two
# separate posts with different URLs (e.g. a redgifs link in one sub and a
# v.redd.it link in another), both will download. Detecting that would require
# downloading and fingerprinting the video files themselves.

from myjdapi import Myjdapi
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

REQUEST_TIMEOUT = 15  # seconds; fail fast instead of hanging

# Load environment variables from .env file
load_dotenv()

# Fetch credentials and configurations from the .env file
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')
DEVICE_NAME = os.getenv('DEVICE_NAME')
CLIENT_ID = os.getenv('CLIENT_ID')
SECRET_TOKEN = os.getenv('SECRET_TOKEN')
REFRESH_TOKEN = os.getenv('REFRESH_TOKEN')
REDDIT_USERNAME = os.getenv('REDDIT_USERNAME')
DESTINATION_FOLDER = os.getenv('DESTINATION_FOLDER')
PUSHOVER_USER_KEY = os.getenv('PUSHOVER_USER_KEY')
PUSHOVER_API_TOKEN = os.getenv('PUSHOVER_API_TOKEN')

# History file lives next to this script, not wherever the process was started
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "sent_links.txt")  # File to store previously sent links
LOG_FILE = os.path.join(SCRIPT_DIR, "redsaves.log")  # Run log: errors + key events
LOG_MAX_BYTES = 1048576  # 1 MB; rotates to redsaves.log.1 when exceeded
AUTH_FAILURE_FILE = os.path.join(SCRIPT_DIR, "auth_failures.txt")  # Watchdog state
WATCHDOG_THRESHOLD = 3  # Pushover alert every this many consecutive Reddit auth failures

# Ensure all required variables are set
if not all([EMAIL, PASSWORD, DEVICE_NAME, CLIENT_ID, SECRET_TOKEN, REFRESH_TOKEN,
            REDDIT_USERNAME, DESTINATION_FOLDER, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN]):
    raise EnvironmentError("Missing required environment variables in .env file.")

# Ensure the history file exists
if not os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, "w") as f:
        pass

# --- Logging: console + rotating file ---------------------------------------
def _write_log(line):
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # console already got it; never crash a run over logging

def log_info(message):
    _write_log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: {message}")

def log_error(message):
    _write_log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {message}")

def rotate_log_if_needed():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
            backup = LOG_FILE + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(LOG_FILE, backup)
            log_info("Log rotated.")
    except OSError as e:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: Log rotation failed: {e}")


# --- Reddit auth watchdog ----------------------------------------------------
def get_auth_failure_count():
    try:
        with open(AUTH_FAILURE_FILE, "r") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0

def set_auth_failure_count(n):
    try:
        with open(AUTH_FAILURE_FILE, "w") as f:
            f.write(str(n))
    except OSError as e:
        log_error(f"Error writing auth failure state: {e}")

def reset_auth_failures():
    if get_auth_failure_count() != 0:
        set_auth_failure_count(0)
        log_info("Reddit auth recovered; failure counter reset.")

def record_auth_failure():
    count = get_auth_failure_count() + 1
    set_auth_failure_count(count)
    log_error(f"Reddit authentication failed ({count} consecutive failure(s)).")
    if count % WATCHDOG_THRESHOLD == 0:
        send_pushover_notification(
            f"RedSaves: Reddit authentication has failed {count} runs in a row. "
            "Your refresh token may have expired - check your .env file."
        )
    return count

# Authenticate with Reddit
def get_access_token():
    try:
        auth = requests.auth.HTTPBasicAuth(CLIENT_ID, SECRET_TOKEN)
        data = {'grant_type': 'refresh_token', 'refresh_token': REFRESH_TOKEN}
        headers = {'User-Agent': 'RedSaves v2.4'}
        response = requests.post('https://www.reddit.com/api/v1/access_token', auth=auth,
                                 data=data, headers=headers, timeout=REQUEST_TIMEOUT)
        return response.json().get('access_token')
    except Exception as e:
        log_error(f"Error obtaining access token: {e}")
        return None

# Fetch saved posts from Reddit
def fetch_saved_posts(token):
    try:
        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.4'}
        response = requests.get(f'https://oauth.reddit.com/user/{REDDIT_USERNAME}/saved',
                                headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code == 200:
            return response.json()
        else:
            log_error(f"Failed to fetch saved posts for user '{REDDIT_USERNAME}'. Status code: {response.status_code}")
            return None
    except Exception as e:
        log_error(f"Error fetching saved posts: {e}")
        return None

# Extract media links (GIFs and videos) from saved posts
def extract_media_links(saved_posts):
    media_links = []
    try:
        for post in saved_posts['data']['children']:
            url = post['data'].get('url', '')
            if any(ext in url for ext in ['.gif', '.mp4', 'redgifs', 'v.redd.it']):
                media_links.append(url)
    except Exception as e:
        log_error(f"Error extracting media links: {e}")
    # Dedupe within this run, preserving order, so the same URL twice in one
    # batch is only sent once.
    return list(dict.fromkeys(media_links))

# Filter out links that have already been sent
def filter_new_links(links):
    try:
        with open(HISTORY_FILE, "r") as f:
            sent_links = set(f.read().splitlines())
        new_links = [link for link in links if link not in sent_links]
        return new_links
    except Exception as e:
        log_error(f"Error filtering new links: {e}")
        return links

# Save new links to the history file
def save_links_to_history(links):
    try:
        with open(HISTORY_FILE, "a") as f:
            for link in links:
                f.write(link + "\n")
    except Exception as e:
        log_error(f"Error saving links to history: {e}")

# Send links to JDownloader
def send_to_jdownloader(links):
    try:
        api = Myjdapi()
        api.connect(EMAIL, PASSWORD)
        device = api.get_device(DEVICE_NAME)
        if device:
            response = device.linkgrabber.add_links([{
                "autostart": True,
                "links": "\n".join(links),
                "packageName": "RedditMedia",
                "destinationFolder": DESTINATION_FOLDER
            }])
            log_info(f"Links sent to JDownloader: {response}")
            return len(links)  # Return the number of links sent
        else:
            log_error("JDownloader device not found.")
            return 0
    except Exception as e:
        log_error(f"Error sending links to JDownloader: {e}")
        return 0

# Send a Pushover notification using the requests library
def send_pushover_notification(message):
    try:
        url = "https://api.pushover.net/1/messages.json"
        payload = {
            "token": PUSHOVER_API_TOKEN,
            "user": PUSHOVER_USER_KEY,
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

# Main execution
if __name__ == "__main__":
    rotate_log_if_needed()
    token = get_access_token()
    if token:
        reset_auth_failures()
        saved_posts = fetch_saved_posts(token)
        if saved_posts:
            media_links = extract_media_links(saved_posts)
            if media_links:
                new_links = filter_new_links(media_links)
                if new_links:
                    log_info(f"Found {len(new_links)} new media links.")
                    count = send_to_jdownloader(new_links)
                    if count > 0:
                        # Only mark links as sent when JDownloader accepted them
                        save_links_to_history(new_links)
                        send_pushover_notification(f"{count} new media links have been sent to JDownloader!")
                    else:
                        log_error("JDownloader send failed; links kept out of history for retry next run.")
                        send_pushover_notification("Failed to send media links to JDownloader.")
                else:
                    log_info("No new media links to send.")
                    send_pushover_notification("No new media links found.")
            else:
                log_info("No media links found.")
                send_pushover_notification("No media links found in saved posts.")
        else:
            log_info("No saved posts fetched.")
            send_pushover_notification("Failed to fetch saved posts.")
    else:
        # Reddit auth failed: count it; the watchdog alerts if this keeps happening
        record_auth_failure()
