"""Stash plugin: pure-function tests (parse_input, build_scene_update). The
plugin itself is stdlib-only and import-safe."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "stash-plugin" / "redsaves"))

import redsaves  # noqa: E402


def test_parse_input_hook():
    # real Stash shape: hookContext nested under args
    kind, ctx = redsaves.parse_input(
        {"server_connection": {},
         "args": {"hookContext": {"id": 42, "type": "Scene.Create.Post"}}})
    assert kind == "hook" and ctx["id"] == 42


def test_parse_input_hook_bare():
    kind, ctx = redsaves.parse_input(
        {"hookContext": {"id": "42", "type": "Scene.Create.Post"}})
    assert kind == "hook" and ctx["id"] == "42"


def test_parse_input_task():
    kind, args = redsaves.parse_input(
        {"server_connection": {}, "args": {"mode": "backfill"}})
    assert kind == "task" and args["mode"] == "backfill"


def test_parse_input_unknown():
    assert redsaves.parse_input({}) == (None, None)
    assert redsaves.parse_input("nope") == (None, None)
    assert redsaves.parse_input(None) == (None, None)


def test_build_scene_update_only_sets_known_fields():
    meta = {"title": "T", "performer": "c1", "tags": ["a"],
            "url": "https://x", "rating100": None, "date": None,
            "details": "u/a"}
    v = redsaves.build_scene_update("7", meta, ["p1"], ["t1"])
    assert v["input"] == {
        "id": "7", "title": "T", "url": "https://x", "details": "u/a",
        "performer_ids": ["p1"], "tag_ids": ["t1"],
    }


def test_build_scene_update_rating_and_empty():
    v = redsaves.build_scene_update("7", {"rating100": 100}, [], [])
    assert v["input"] == {"id": "7", "rating100": 100}
    v = redsaves.build_scene_update("7", {"found": False}, [], [])
    assert v["input"] == {"id": "7"}


def test_manifest_exists_and_names_plugin():
    yml = (Path(__file__).resolve().parent.parent
           / "stash-plugin" / "redsaves" / "redsaves.yml")
    text = yml.read_text()
    assert "Scene.Create.Post" in text
    assert "redsaves_url" in text
    assert "Backfill from RedSaves" in text


def test_path_in_prefix():
    assert redsaves.path_in_prefix("/data/reddit/clip.mp4", "/data/reddit")
    assert redsaves.path_in_prefix("/data/reddit/sub/clip.mp4", "/data/reddit/")
    assert not redsaves.path_in_prefix("/data/other/clip.mp4", "/data/reddit")
    assert not redsaves.path_in_prefix("/data/reddit2/clip.mp4", "/data/reddit")
    assert redsaves.path_in_prefix("/data/reddit/clip.mp4", "")
    assert redsaves.path_in_prefix("/data/reddit/clip.mp4", None)


def test_find_or_create_tag_resolves_alias(monkeypatch):
    import redsaves
    created = []

    def fake_gql(query, variables=None):
        return {"findTags": {"tags": []}}  # never exists

    def fake_gql_raw(query, variables=None):
        name = variables["name"]
        created.append(name)
        if name == "Smile":
            return {"errors": [{"message":
                "name 'Smile' is used as alias for 'Smiling'"}]}
        return {"data": {"tagCreate": {"id": "t99"}}}

    monkeypatch.setattr(redsaves, "gql", fake_gql)
    monkeypatch.setattr(redsaves, "gql_raw", fake_gql_raw)
    # second call: FIND_TAG for 'Smiling' still misses, then create succeeds
    assert redsaves.find_or_create_tag("Smile") == "t99"
    assert created == ["Smile", "Smiling"]


def test_find_or_create_tag_create_failure_returns_none(monkeypatch):
    import redsaves
    monkeypatch.setattr(redsaves, "gql", lambda q, v=None: {"findTags": {"tags": []}})
    monkeypatch.setattr(redsaves, "gql_raw",
                        lambda q, v=None: {"errors": [{"message": "boom"}]})
    assert redsaves.find_or_create_tag("Nope") is None


def test_alias_index_loads_names_and_aliases(monkeypatch):
    import redsaves

    def fake_gql(query, variables=None):
        if "AllTags" in query:
            return {"findTags": {"tags": [
                {"id": "t1", "name": "Smiling", "aliases": ["Smile"]},
            ]}}
        return {"findPerformers": {"performers": []}}

    monkeypatch.setattr(redsaves, "gql", fake_gql)
    idx = redsaves.AliasIndex()
    idx.load("tag")
    assert idx.get("Smile") == "t1"
    assert idx.get("smiling") == "t1"
    assert idx.get("Nope") is None


def test_find_or_create_tag_prefers_index(monkeypatch):
    import redsaves
    idx = redsaves.AliasIndex()
    idx.add("Smile", "t5")
    monkeypatch.setattr(redsaves, "_tag_index", idx)

    def boom(*a, **k):
        raise AssertionError("no GraphQL expected")

    monkeypatch.setattr(redsaves, "gql", boom)
    monkeypatch.setattr(redsaves, "gql_raw", boom)
    assert redsaves.find_or_create_tag("smile") == "t5"
