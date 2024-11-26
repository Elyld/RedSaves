# RedSaves 2.1

from myjdapi import Myjdapi
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

# Load environment variables from .env file
load_dotenv()

# Fetch credentials and configurations from the .env file
EMAIL = os.getenv('EMAIL')
PASSWORD = os.getenv('PASSWORD')
DEVICE_NAME = os.getenv('DEVICE_NAME')
CLIENT_ID = os.getenv('CLIENT_ID')
SECRET_TOKEN = os.getenv('SECRET_TOKEN')
REFRESH_TOKEN = os.getenv('REFRESH_TOKEN')
DESTINATION_FOLDER = os.getenv('DESTINATION_FOLDER')
PUSHOVER_USER_KEY = os.getenv('PUSHOVER_USER_KEY')
PUSHOVER_API_TOKEN = os.getenv('PUSHOVER_API_TOKEN')
HISTORY_FILE = "sent_links.txt"  # File to store previously sent links

# Ensure all required variables are set
if not all([EMAIL, PASSWORD, DEVICE_NAME, CLIENT_ID, SECRET_TOKEN, REFRESH_TOKEN, DESTINATION_FOLDER, PUSHOVER_USER_KEY, PUSHOVER_API_TOKEN]):
    raise EnvironmentError("Missing required environment variables in .env file.")

# Ensure the history file exists
if not os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, "w") as f:
        pass

# Log errors to the console
def log_error(message):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {message}")

# Authenticate with Reddit
def get_access_token():
    try:
        auth = requests.auth.HTTPBasicAuth(CLIENT_ID, SECRET_TOKEN)
        data = {'grant_type': 'refresh_token', 'refresh_token': REFRESH_TOKEN}
        headers = {'User-Agent': 'RedSaves v2.1'}
        response = requests.post('https://www.reddit.com/api/v1/access_token', auth=auth, data=data, headers=headers)
        return response.json().get('access_token')
    except Exception as e:
        log_error(f"Error obtaining access token: {e}")
        return None

# Fetch saved posts from Reddit
def fetch_saved_posts(token):
    try:
        username = os.getenv('REDDIT_USERNAME')  # Fetch username from .env file
        if not username:
            raise EnvironmentError("Missing Reddit username in environment variables.")

        headers = {'Authorization': f'bearer {token}', 'User-Agent': 'RedSaves v2.1'}
        response = requests.get(f'https://oauth.reddit.com/user/{username}/saved', headers=headers)
        if response.status_code == 200:
            return response.json()
        else:
            log_error(f"Failed to fetch saved posts for user '{username}'. Status code: {response.status_code}")
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
            if any(ext in url for ext in ['.gif', '.mp4', 'redgifs']):
                media_links.append(url)
    except Exception as e:
        log_error(f"Error extracting media links: {e}")
    return media_links

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
            print(f"Links sent to JDownloader: {response}")
            return len(links)  # Return the number of links sent
        else:
            print("JDownloader device not found.")
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
        response = requests.post(url, data=payload)
        if response.status_code == 200:
            print("Pushover notification sent successfully.")
        else:
            log_error(f"Failed to send Pushover notification. Status code: {response.status_code}")
    except Exception as e:
        log_error(f"Error sending Pushover notification: {e}")

# Main execution
if __name__ == "__main__":
    token = get_access_token()
    if token:
        saved_posts = fetch_saved_posts(token)
        if saved_posts:
            media_links = extract_media_links(saved_posts)
            if media_links:
                new_links = filter_new_links(media_links)
                if new_links:
                    print(f"Found {len(new_links)} new media links.")
                    count = send_to_jdownloader(new_links)
                    save_links_to_history(new_links)
                    send_pushover_notification(f"{count} new media links have been sent to JDownloader!")
                else:
                    print("No new media links to send.")
                    send_pushover_notification("No new media links found.")
            else:
                print("No media links found.")
                send_pushover_notification("No media links found in saved posts.")
        else:
            print("No saved posts fetched.")
            send_pushover_notification("Failed to fetch saved posts.")