#!/usr/bin/env python3
"""RedSaves metadata for Stash.

Listens for Scene.Create.Post (a new file scanned in) and fills the scene's
metadata from your RedSaves library: title, performer, tags, URL, rating and
date. Also provides a "Backfill from RedSaves" task for existing scenes.

Never touches files — metadata only, through Stash's GraphQL API.

Stdlib only: no pip packages needed.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PLUGIN_ID = "redsaves"
STASH_GRAPHQL = "http://localhost:9999/graphql"
MARKER_TAG = "RedSaves"  # added to every processed scene; skips re-processing
MAX_TAGS = 12
_ALIAS_RE = re.compile(r"is used as alias for '([^']+)'")
_OP_RE = re.compile(r"(?:query|mutation)\s+(\w+)")


def _op_name(query):
    m = _OP_RE.search(query or "")
    return m.group(1) if m else "graphql"

_api_key = ""


def log(msg):
    # stdout -> Stash logs this as Info
    print(f"[redsaves] {msg}", flush=True)


def log_err(msg):
    # stderr -> Stash logs this as Error; reserve for real failures
    print(f"[redsaves] {msg}", file=sys.stderr, flush=True)


# --- Stash GraphQL ---------------------------------------------------------------

def gql_raw(query, variables=None):
    """Full response body (data + errors), or None on transport failure."""
    op = _op_name(query)
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        STASH_GRAPHQL, data=payload,
        headers={"Content-Type": "application/json"})
    if _api_key:
        req.add_header("ApiKey", _api_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        log_err(f"{op}: GraphQL HTTP {e.code}")
    except Exception as e:  # timeouts, connection refused, ...
        log_err(f"{op}: GraphQL error: {e}")
    return None


def gql(query, variables=None):
    op = _op_name(query)
    body = gql_raw(query, variables)
    if not body:
        return None
    if body.get("errors"):
        log_err(f"{op}: GraphQL errors: {json.dumps(body['errors'])[:300]}")
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
query FindScenes($page: Int!, $scene_filter: SceneFilterType) {
  findScenes(filter: {page: $page, per_page: 100}, scene_filter: $scene_filter) {
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

ALL_TAGS = """
query AllTags($page: Int!) {
  findTags(filter: {page: $page, per_page: 500}) {
    tags { id name aliases }
  }
}"""

ALL_PERFORMERS = """
query AllPerformers($page: Int!) {
  findPerformers(filter: {page: $page, per_page: 500}) {
    performers { id name aliases }
  }
}"""


class AliasIndex:
    """Canonical id lookup by exact name or alias (case-insensitive).

    Loaded once at startup so tag/performer creation is never attempted for
    a name Stash already knows as an alias — those mutations always fail and
    spam the log.
    """

    def __init__(self):
        self._map = {}

    def load(self, kind):
        query = ALL_TAGS if kind == "tag" else ALL_PERFORMERS
        outer = "findTags" if kind == "tag" else "findPerformers"
        inner = "tags" if kind == "tag" else "performers"
        page = 1
        while True:
            data = gql(query, {"page": page})
            items = ((data or {}).get(outer) or {}).get(inner) or []
            if not items:
                break
            for t in items:
                self._map[t["name"].lower()] = t["id"]
                for a in t.get("aliases") or []:
                    self._map.setdefault(a.lower(), t["id"])
            if len(items) < 500:
                break
            page += 1

    def get(self, name):
        return self._map.get((name or "").lower())

    def add(self, name, id_):
        self._map[(name or "").lower()] = id_


_tag_index = AliasIndex()
_performer_index = AliasIndex()


def plugin_settings():
    """Settings from Stash (Settings -> Plugins), with an optional local
    config.json next to this script as an override / escape hatch."""
    settings = {}
    data = gql(CONFIGURATION)
    try:
        plugins = ((data or {}).get("configuration") or {}).get("plugins") or {}
        if isinstance(plugins, str):
            plugins = json.loads(plugins)
        settings = plugins.get(PLUGIN_ID) or {}
    except Exception as e:
        log_err(f"could not read plugin settings: {e}")
    try:
        with open("config.json", encoding="utf-8") as f:
            settings.update(json.load(f))
    except OSError:
        pass
    except Exception as e:
        log_err(f"bad config.json: {e}")
    return settings


def _create_named(create_query, name, kind):
    """Create a tag/performer; if Stash rejects the name as an alias of an
    existing one, resolve to the canonical entry instead."""
    body = gql_raw(create_query, {"name": name})
    if not body:
        return None
    created = (body.get("data") or {}).get(
        "tagCreate" if kind == "tag" else "performerCreate") or {}
    if created.get("id"):
        return created["id"]
    errs = json.dumps(body.get("errors") or "")
    m = _ALIAS_RE.search(errs)
    if m:
        canonical = m.group(1)
        log(f"{kind} '{name}' is an alias of '{canonical}' - using that")
        if kind == "tag":
            return find_or_create_tag(canonical)
        return find_or_create_performer(canonical)
    log_err(f"{kind}Create failed for '{name}': {errs[:200]}")
    return None


def find_or_create_performer(name):
    pid = _performer_index.get(name)
    if pid:
        return pid
    data = gql(FIND_PERFORMER, {"name": name})
    performers = ((data or {}).get("findPerformers") or {}).get("performers") or []
    if performers:
        _performer_index.add(name, performers[0]["id"])
        return performers[0]["id"]
    pid = _create_named(PERFORMER_CREATE, name, "performer")
    if pid:
        _performer_index.add(name, pid)
    return pid


def find_or_create_tag(name):
    tid = _tag_index.get(name)
    if tid:
        return tid
    data = gql(FIND_TAG, {"name": name})
    tags = ((data or {}).get("findTags") or {}).get("tags") or []
    if tags:
        _tag_index.add(name, tags[0]["id"])
        return tags[0]["id"]
    tid = _create_named(TAG_CREATE, name, "tag")
    if tid:
        _tag_index.add(name, tid)
    return tid


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
        log_err(f"RedSaves lookup failed for {filename}: {e}")
        return None
    return meta if meta.get("found") else None


# --- core --------------------------------------------------------------------------

def path_in_prefix(path, prefix):
    """True if path is the prefix dir itself or inside it."""
    prefix = (prefix or "").rstrip("/")
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


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


def process_scene(scene_id, base_url, path_prefix="", dry_run=False):
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
    if not path_in_prefix(path, path_prefix):
        return False  # outside the configured directory
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


def backfill(base_url, path_prefix="", dry_run=False):
    page, updated, total = 1, 0, None
    scene_filter = None
    if (path_prefix or "").strip():
        scene_filter = {"path": {"value": path_prefix.rstrip("/"),
                                 "modifier": "INCLUDES"}}
    while True:
        data = gql(FIND_SCENES, {"page": page, "scene_filter": scene_filter})
        block = (data or {}).get("findScenes") or {}
        scenes = block.get("scenes") or []
        total = block.get("count") or 0
        for scene in scenes:
            tag_names = {t["name"] for t in scene.get("tags") or []}
            if MARKER_TAG in tag_names:
                continue
            if process_scene(scene["id"], base_url, path_prefix, dry_run):
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
    path_prefix = (settings.get("path_prefix") or "").strip()
    dry_run = bool(settings.get("dry_run"))
    if not base_url:
        log_err("Set 'RedSaves URL' in the plugin settings "
            "(e.g. http://192.168.1.10:4003).")
        return

    _tag_index.load("tag")
    _performer_index.load("performer")

    if kind == "hook":
        scene_id = payload.get("id")
        if scene_id:
            process_scene(str(scene_id), base_url, path_prefix, dry_run)
    elif kind == "task" and payload.get("mode") == "backfill":
        backfill(base_url, path_prefix, dry_run)
    else:
        log(f"ignoring input: {json.dumps(raw)[:120]}")


if __name__ == "__main__":
    main()
