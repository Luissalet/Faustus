"""Versioned repository catalogue and shipped manifests; no clones or network."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from src import plugins

ROOT = Path(__file__).parents[1] / "plugins"
CATALOG = json.loads((ROOT / "marketplace.json").read_text(encoding="utf-8"))


def test_catalog_schema_unique_ids_and_public_clone_urls():
    assert CATALOG["schema"] == 1
    entries = CATALOG["plugins"]
    assert len(entries) == 31
    assert len({entry["id"] for entry in entries}) == len(entries)
    for entry in entries:
        assert set(entry) == {"id", "repository_url", "required"}
        assert type(entry["required"]) is bool
        url = urlsplit(entry["repository_url"])
        assert url.scheme == "https" and url.hostname == "github.com"
        assert url.username is None and url.password is None
        assert not url.query and not url.fragment
        assert url.path.endswith(".git") and len(url.path.strip("/").split("/")) == 2


def test_required_entry_is_the_repository_that_supplies_hoardlink_and_hub():
    required = [entry for entry in CATALOG["plugins"] if entry["required"]]
    assert required == [{"id": "hoardhub", "repository_url": "https://github.com/Luissalet/HoardLink.git", "required": True}]
    plugin = plugins.parse_manifest(json.loads((ROOT / "hoardhub" / "plugin.json").read_text(encoding="utf-8")))
    assert "HOARDLINK_DIR" in plugin.placeholders
    assert "hoard_link.hub" in plugin.launch_hint["argv"]


@pytest.mark.parametrize("entry", CATALOG["plugins"], ids=lambda entry: entry["id"])
def test_each_catalog_entry_has_a_real_valid_shipped_manifest(entry):
    manifest = ROOT / entry["id"] / "plugin.json"
    parsed = plugins.parse_manifest(json.loads(manifest.read_text(encoding="utf-8")), path=str(manifest))
    assert parsed.id == entry["id"]


def test_new_manifests_load_via_real_loader_without_user_data(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "builtin_dir", lambda: str(ROOT))
    monkeypatch.setattr(plugins, "user_dir", lambda: str(tmp_path / "empty-user-plugins"))
    loaded = plugins.load_all()
    assert loaded.errors == [] and loaded.shadowed == []
    assert {entry["id"] for entry in CATALOG["plugins"]} <= set(loaded.plugins)


def test_verified_home_and_story_repositories_are_optional_catalog_entries():
    entries = {entry["id"]: entry for entry in CATALOG["plugins"]}
    assert entries["homehoard"] == {"id": "homehoard",
        "repository_url": "https://github.com/Luissalet/HomeHoard.git", "required": False}
    assert entries["scheherazade"] == {"id": "scheherazade",
        "repository_url": "https://github.com/Luissalet/ScheherazadesHoard.git", "required": False}
    assert entries["tantalus"] == {"id": "tantalus",
        "repository_url": "https://github.com/Luissalet/TantalusHoard.git", "required": False}


def test_independent_watch_and_book_repositories_are_excluded():
    entries = CATALOG["plugins"]
    independent = {"watch", "watchhoard", "book", "bookhoard", "mybookhoard"}
    assert independent.isdisjoint({entry["id"].casefold() for entry in entries})
    repository_names = {urlsplit(entry["repository_url"]).path.rsplit("/", 1)[-1]
        .removesuffix(".git").casefold() for entry in entries}
    assert independent.isdisjoint(repository_names)


@pytest.mark.parametrize("plugin_id,entrypoint", [("cookhoard", "apps/mcp/bootstrap.mjs"),
    ("gamerhoard", "mcp/server.mjs")])
def test_direct_stdio_toolpacks_have_no_fabricated_background_app(plugin_id, entrypoint):
    raw = json.loads((ROOT / plugin_id / "plugin.json").read_text(encoding="utf-8"))
    plugin = plugins.parse_manifest(raw)
    assert "app" not in raw
    assert plugin.app_url_default == "" and plugin.health_path == ""
    assert plugin.identify == {} and plugin.launch_hint == {}
    assert plugin.transport == "stdio" and plugin.command == "node"
    assert plugin.args[0].endswith(entrypoint)
