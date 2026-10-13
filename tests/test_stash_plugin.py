"""Stash plugin: pure-function tests (parse_input, build_scene_update). The
plugin itself is stdlib-only and import-safe."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "stash-plugin" / "redsaves"))

import redsaves  # noqa: E402


def test_parse_input_hook():
    kind, ctx = redsaves.parse_input(
        {"hookContext": {"id": "42", "type": "Scene.Create.Post"}})
    assert kind == "hook" and ctx["id"] == "42"


def test_parse_input_task():
    kind, args = redsaves.parse_input({"args": {"mode": "backfill"}})
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
