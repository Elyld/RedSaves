# RedSaves Metadata — Stash plugin

Fills scene metadata from your RedSaves library: title, performer (redgifs
creator), tags (subreddit, flair, redgifs tags), the original Reddit URL,
rating (from your ★ favorites), and date.

**It never touches your files** — no moving, no renaming. Metadata only,
written through Stash's API. It also never scrapes redgifs; RedSaves already
knows the exact post each file came from.

## Install

1. Copy this `redsaves` folder into Stash's plugins directory (the `plugins`
   folder inside your Stash config directory on Unraid).
2. Stash → **Settings → Plugins → Reload plugins**.
3. Open the plugin's settings and set **RedSaves URL** to your RedSaves WebUI
   as seen from the Stash server, e.g. `http://192.168.1.10:4003`. Use the
   Unraid IP — `localhost` only works if RedSaves runs on the same machine
   as Stash.
4. If your Stash requires API key authentication, paste the key into the
   plugin's **Stash API key** setting too.
5. Optionally tick **Dry run** first and run a scan — check Stash's log to
   see what it *would* change, then untick it.

## Use

- **New scenes:** the `Scene.Create.Post` hook fires automatically during
  Stash scans. Files RedSaves doesn't know are skipped silently.
- **Existing library:** Settings → Tasks → Plugin Tasks → **Backfill from
  RedSaves** → Run. Every processed scene gets a `RedSaves` tag, so re-runs
  only pick up new ones. Set **Only this directory** in the plugin settings
  first if you only want one folder covered (use the path as seen inside
  the Stash container, e.g. `/data/reddit`) — the new-scan hook respects it
  too.

## Manual config

If the settings UI isn't reachable, create a `config.json` next to
`redsaves.py` (it overrides the UI settings):

```json
{
  "redsaves_url": "http://192.168.1.10:4003",
  "api_key": "",
  "dry_run": false
}
```

## Requirements

- Python 3 (included in the official Stash Docker image) — stdlib only, no
  pip packages.
- RedSaves v2.14.0+ (provides the `/api/stash/lookup` endpoint).
