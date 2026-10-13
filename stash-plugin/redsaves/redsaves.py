#!/usr/bin/env python3
"""RedSaves metadata for Stash.

Listens for Scene.Create.Post (a new file scanned in) and fills the scene's
metadata from your RedSaves library: title, performer, tags, URL, rating and
date. Also provides a "Backfill from RedSaves" task for existing scenes.

Never touches files — metadata only, through Stash's GraphQL API.

Stdlib only: no pip packages needed.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

PLUGIN_ID = "redsaves"
STASH_GRAPHQL = "http://localhost:9999/graphql"
MARKER_TAG = "RedSaves"  # added to every processed scene; skips re-processing
MAX_TAGS = 12

_api_key = ""


def log(msg):
    print(f"[redsaves] {msg}", file=sys.stderr, flush=True)


# --- Stash GraphQL ---------------------------------------------------------------

def gql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        STASH_GRAPHQL, data=payload,
        headers={"Content-Type": "application/json"})
    if _api_key:
        req.add_header("ApiKey", _api_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        log(f"GraphQL HTTP {e.code}")
        return None
    except Exception as e:  # timeouts, connection refused, ...
        log(f"GraphQL error: {e}")
        return None
    if body.get("errors"):
        log(f"GraphQL errors: {json.dumps(body['errors'])[:300]}")
        return None
    return body.get("data")


FIND_SCENE = """
query FindScene($id: ID!) {
  findScene(id: $id) {
    id files { path }
    performers { id } tags { id name }
  }
}"""

FIND_SCENES = """
query FindScenes($page: Int!) {
  findScenes(filter: {page: $page, per_page: 100}) {
    count scenes { id files { path } tags { name } }
  }
}"""

SCENE_UPDATE = """
mutation SceneUpdate($input: SceneUpdateInput!) {
  sceneUpdate(input: $input) { id }
}"""

FIND_PERFORMER = """
query FindPerformer($name: String!) {
  findPerformers(performer_filter: {name: {value: $name, modifier: EQUALS}}) {
    performers { id }
  }
}"""

PERFORMER_CREATE = """
mutation PerformerCreate($name: String!) {
  performerCreate(input: {name: $name}) { id }
}"""

FIND_TAG = """
query FindTag($name: String!) {
  findTags(tag_filter: {name: {value: $name, modifier: EQUALS}}) {
    tags { id }
  }
}"""

TAG_CREATE = """
mutation TagCreate($name: String!) {
  tagCreate(input: {name: $name}) { id }
}"""

CONFIGURATION = "query { configuration { plugins } }"


def plugin_settings():
    """Settings from Stash (Settings -> Plugins), with an optional local
    config.json next to this script as an override / escape hatch."""
    settings = {}
    data = gql(CONFIGURATION)
    try:
        plugins = json.loads(
            ((data or {}).get("configuration") or {}).get("plugins") or "{}")
        settings = plugins.get(PLUGIN_ID) or {}
    except Exception as e:
        log(f"could not read plugin settings: {e}")
    try:
        with open("config.json", encoding="utf-8") as f:
            settings.update(json.load(f))
    except OSError:
        pass
    except Exception as e:
        log(f"bad config.json: {e}")
    return settings


def find_or_create_performer(name):
    data = gql(FIND_PERFORMER, {"name": name})
    performers = ((data or {}).get("findPerformers") or {}).get("performers") or []
    if performers:
        return performers[0]["id"]
    data = gql(PERFORMER_CREATE, {"name": name})
    return ((data or {}).get("performerCreate") or {}).get("id")


def find_or_create_tag(name):
    data = gql(FIND_TAG, {"name": name})
    tags = ((data or {}).get("findTags") or {}).get("tags") or []
    if tags:
        return tags[0]["id"]
    data = gql(TAG_CREATE, {"name": name})
    return ((data or {}).get("tagCreate") or {}).get("id")


# --- RedSaves --------------------------------------------------------------------

def redsaves_lookup(base_url, filename):
    """Ask RedSaves what it knows about a file. Returns the meta dict or
    None. Never raises — a down RedSaves must not break Stash's scan."""
    try:
        url = base_url + "/api/stash/lookup?" + urllib.parse.urlencode(
            {"filename": filename})
        with urllib.request.urlopen(url, timeout=15) as resp:
            meta = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        log(f"RedSaves lookup failed for {filename}: {e}")
        return None
    return meta if meta.get("found") else None


# --- core --------------------------------------------------------------------------

def parse_input(raw):
    """('hook', hookContext) | ('task', args) | (None, None)."""
    if not isinstance(raw, dict):
        return None, None
    if isinstance(raw.get("hookContext"), dict):
        return "hook", raw["hookContext"]
    if isinstance(raw.get("args"), dict):
        return "task", raw["args"]
    return None, None


def build_scene_update(scene_id, meta, performer_ids, tag_ids):
    """Pure: GraphQL variables for sceneUpdate. Only sets fields we have, so
    nothing the user already filled in gets clobbered."""
    inp = {"id": scene_id}
    for key in ("title", "details", "url", "date"):
        if meta.get(key):
            inp[key] = meta[key]
    if meta.get("rating100"):
        inp["rating100"] = meta["rating100"]
    if performer_ids:
        inp["performer_ids"] = performer_ids
    if tag_ids:
        inp["tag_ids"] = tag_ids
    return {"input": inp}


def process_scene(scene_id, base_url, dry_run=False):
    data = gql(FIND_SCENE, {"id": scene_id})
    scene = (data or {}).get("findScene")
    if not scene:
        return False
    tag_names = {t["name"] for t in scene.get("tags") or []}
    if MARKER_TAG in tag_names:
        return False  # already handled
    files = scene.get("files") or []
    if not files:
        return False
    path = files[0].get("path") or ""
    filename = path.replace("\\", "/").rsplit("/", 1)[-1]
    meta = redsaves_lookup(base_url, filename)
    if not meta:
        log(f"no RedSaves match for {filename}")
        return False

    performer_ids = [p["id"] for p in scene.get("performers") or []]
    if meta.get("performer") and not dry_run:
        pid = find_or_create_performer(meta["performer"])
        if pid and pid not in performer_ids:
            performer_ids.append(pid)
    elif meta.get("performer"):
        performer_ids.append("dry-run-performer")

    tag_ids = [t["id"] for t in scene.get("tags") or []]
    for name in list((meta.get("tags") or [])[:MAX_TAGS]) + [MARKER_TAG]:
        if dry_run:
            continue
        tid = find_or_create_tag(name)
        if tid and tid not in tag_ids:
            tag_ids.append(tid)

    variables = build_scene_update(scene_id, meta, performer_ids, tag_ids)
    if dry_run:
        log(f"DRY RUN {filename}: would set {json.dumps(variables['input'])}")
        return True
    ok = gql(SCENE_UPDATE, variables) is not None
    log(f"{'updated' if ok else 'FAILED'} {filename}")
    return ok


def backfill(base_url, dry_run=False):
    page, updated, total = 1, 0, None
    while True:
        data = gql(FIND_SCENES, {"page": page})
        block = (data or {}).get("findScenes") or {}
        scenes = block.get("scenes") or []
        total = block.get("count") or 0
        for scene in scenes:
            tag_names = {t["name"] for t in scene.get("tags") or []}
            if MARKER_TAG in tag_names:
                continue
            if process_scene(scene["id"], base_url, dry_run):
                updated += 1
        if page * 100 >= total or not scenes:
            break
        page += 1
    log(f"backfill finished: {updated} scene(s) updated of {total}")


def main():
    try:
        raw = json.load(sys.stdin)
    except Exception:
        raw = {}
    kind, payload = parse_input(raw)

    global _api_key
    settings = plugin_settings()
    _api_key = settings.get("api_key") or ""
    base_url = (settings.get("redsaves_url") or "").rstrip("/")
    dry_run = bool(settings.get("dry_run"))
    if not base_url:
        log("Set 'RedSaves URL' in the plugin settings "
            "(e.g. http://192.168.1.10:4003).")
        return

    if kind == "hook":
        scene_id = payload.get("id")
        if scene_id:
            process_scene(str(scene_id), base_url, dry_run)
    elif kind == "task" and payload.get("mode") == "backfill":
        backfill(base_url, dry_run)
    else:
        log(f"ignoring input: {json.dumps(raw)[:120]}")


if __name__ == "__main__":
    main()
