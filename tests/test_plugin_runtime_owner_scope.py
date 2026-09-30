"""An empty owner-visible connector set must stay empty."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from src import plugin_runtime as runtime, launch_profiles


@pytest.fixture
def world(monkeypatch):
    plugin = SimpleNamespace(id="image_fixture", name="Synthetic image plugin", purpose="Fixture only",
        capabilities=["image"], notes="", source="builtin", app_url_default="http://default.invalid",
        health_path="/health")
    other = {"id": "other-connection", "connector_id": "other-alias", "preset_id": plugin.id,
        "owner": "other", "app_url": "http://other.invalid", "ui_url": "http://other.invalid/ui",
        "launch_profile_id": "other-profile"}
    rows = [other]
    monkeypatch.setattr(runtime.plugins_mod, "load_plugins", lambda: {plugin.id: plugin})
    monkeypatch.setattr(runtime, "_connectors", lambda: deepcopy(rows))
    calls = {"health": [], "launch": [], "profiles": []}
    async def health(connector, plugin):
        calls["health"].append(connector["id"])
        return {"reachable": True}
    async def launch(profile_id):
        calls["launch"].append(profile_id)
        return {"launched": True}
    def profile(profile_id):
        calls["profiles"].append(profile_id)
        return {"id": profile_id}
    monkeypatch.setattr(runtime, "app_reachable", health)
    monkeypatch.setattr(launch_profiles, "launch", launch)
    monkeypatch.setattr(launch_profiles, "get_profile", profile)
    return plugin, rows, calls


@pytest.mark.parametrize("ref", ["image_fixture", "Synthetic image plugin", "other-connection", "other-alias"])
def test_owner_cannot_resolve_other_connector_by_any_reference(world, ref):
    found = runtime.resolve(ref, owner="mine")
    assert found["connector"] is None and found["reason"]
    assert "http://other.invalid" not in found["reason"]


def test_checked_survey_does_not_probe_or_advertise_other_owner_connection(world):
    plugin, rows, calls = world
    entry = asyncio.run(runtime.survey(owner="mine", check=True))[0]
    assert entry["plugin"] == plugin.id and not entry["connected"]
    assert entry["connector_id"] is None and entry["launch_profile_id"] is None
    assert not entry["can_start"] and not entry["can_show"]
    assert entry["app_url"] == plugin.app_url_default and "app" not in entry
    assert calls == {"health": [], "launch": [], "profiles": []}


@pytest.mark.parametrize("ref", ["image_fixture", "Synthetic image plugin", "other-connection", "other-alias"])
def test_ensure_running_never_probes_or_launches_other_owner(world, ref):
    _, _, calls = world
    result = asyncio.run(runtime.ensure_running(ref, owner="mine"))
    assert result["ok"] is False
    assert calls == {"health": [], "launch": [], "profiles": []}


@pytest.mark.parametrize("visible_owner", ["mine", None, "", "missing"])
def test_own_and_shared_connectors_remain_visible(world, visible_owner):
    plugin, rows, calls = world
    visible = {**rows[0], "id": "visible", "owner": visible_owner, "app_url": "http://visible.invalid"}
    if visible_owner == "missing":
        visible.pop("owner")
    rows.append(visible)
    assert runtime.resolve(plugin.id, owner="mine")["connector"]["id"] == "visible"
    assert runtime.resolve("visible", owner="mine")["connector"]["id"] == "visible"
    entry = asyncio.run(runtime.survey(owner="mine", check=True))[0]
    assert entry["connected"] and entry["connector_id"] == "visible"
    result = asyncio.run(runtime.ensure_running(plugin.id, owner="mine"))
    assert result["already_running"] and calls["health"] == ["visible", "visible"]
    assert calls["launch"] == []


@pytest.mark.parametrize("owner", [None, ""])
def test_unspecified_owner_keeps_existing_unscoped_contract(world, owner):
    plugin, _, _ = world
    assert runtime.resolve(plugin.id, owner=owner)["connector"]["id"] == "other-connection"
