# RedSaves

Reddit saved-posts → JDownloader, now with a WebUI. Scans your Reddit saved
posts twice daily for media (`.gif`, `.mp4`, redgifs, v.redd.it) and sends new
links to JDownloader, with Pushover notifications. The scan behavior is
unchanged from the standalone `RedSaves2.0.py` script — the container just
gives it a real history database, a dashboard, and its own scheduler.

## Changelog

- **v2.6.0** — JDownloader auto-start (downloads begin immediately), post
  author captured and shown in History, Leaderboard page (top subreddits /
  posters / weekly volume), Logs page, and sent_links.txt import upload.
- **v2.5.0** — Container + WebUI with SQLite history database, own scheduler,
  first-boot sent_links.txt migration.

## Run it (Dockge)

Copy `dockge-compose.yml` into a new Dockge stack as-is, then fill in the
**Environment** section with your credentials (exact variable names — the app
reads these and only these):

| Variable             | What it is                              |
|----------------------|-----------------------------------------|
| `CLIENT_ID`          | Reddit API client ID                    |
| `SECRET_TOKEN`       | Reddit API secret                       |
| `REFRESH_TOKEN`      | Reddit OAuth refresh token              |
| `EMAIL`              | MyJDownloader account email             |
| `PASSWORD`           | MyJDownloader account password          |
| `DEVICE_NAME`        | JDownloader device name                 |
| `DESTINATION_FOLDER` | Download destination folder in JDownloader |
| `REDDIT_USERNAME`    | Reddit username whose saved posts to scan |
| `PUSHOVER_USER_KEY`  | Pushover user key                       |
| `PUSHOVER_API_TOKEN` | Pushover API token                      |

That's it — deploy the stack. The WebUI lands on **port 4003**.

```yaml
# dockge-compose.yml — paste into Dockge, fill in Environment, deploy
services:
  redsaves:
    image: ghcr.io/elyld/redsaves:latest
    container_name: redsaves
    ports:
      - "4003:8000"
    environment:
      - TZ=America/Chicago
      - CLIENT_ID=
      - SECRET_TOKEN=
      - REFRESH_TOKEN=
      - REDDIT_USERNAME=
      - EMAIL=
      - PASSWORD=
      - DEVICE_NAME=
      - DESTINATION_FOLDER=
      - PUSHOVER_USER_KEY=
      - PUSHOVER_API_TOKEN=
    volumes:
      - redsaves-data:/data
    restart: unless-stopped

volumes:
  redsaves-data:
```

**Updating** is just a redeploy — the image is tagged `latest` on every merge
to `main` (plus `vX.Y.Z` tags on releases), so Dockge pulls the new build.

## What the container does

- **Runs the scans itself** — twice daily at 08:00 and 20:00 container-local
  time by default (set `TZ` in the compose file; times are adjustable in the
  WebUI under Settings). This replaces the external scheduler/cron the
  standalone script used. The scan logic is byte-for-byte the v2.4 behavior:
  first 25 saved posts, same media matching, same Pushover messages.
- **History database** — SQLite at `/data/redsaves.db` (kept on the
  `redsaves-data` volume so it survives redeploys). Every link JDownloader
  accepts is recorded with the post title, subreddit, and timestamp.
- **First-boot migration** — if the database is empty, an existing
  `sent_links.txt` (in `/data`, or next to the script) is imported, so
  nothing you've already sent gets re-downloaded. To use it: copy your
  `sent_links.txt` into the volume before first deploy, or place it in
  `/data` on the host bind.
- **WebUI** — dashboard (last/next run, totals, auth watchdog state, Scan
  Now button), searchable history (now with the post author), Top leaderboard
  (top subreddits, top posters, links per week), per-run detail, run logs
  viewer, and settings (scan times, notification toggles). Secrets stay in
  environment variables and are never shown or stored.
- **JDownloader auto-start** — links are moved from LinkGrabber into the
  download list right after sending, so downloads begin immediately instead
  of sitting in the grabber.
- **History import** — upload a `sent_links.txt` (one URL per line) from the
  History page to backfill the database; duplicates are skipped and the page
  reports how many were imported.

## Local dev

```bash
cp .env.example .env   # fill in your values
docker compose up --build
# or: python -m venv .venv && .venv/bin/pip install -r requirements.txt
#     REDSAVES_DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

Tests: `.venv/bin/python -m pytest tests/`

## The standalone script

`RedSaves2.0.py` remains in the repo as the reference implementation of the
scan. The containerized app (`app/scanner.py`) is a DB-backed refactor with
identical behavior; the script itself is unchanged.
