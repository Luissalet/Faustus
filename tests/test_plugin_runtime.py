"""Using a plugin's app, not just talking to it (src/plugin_runtime.py).

The rules being pinned are refusals as much as capabilities. Faustus can
start one of the user's applications when it needs it, and show it when
asked — but it starts only from a launch profile the user already saved, it
never restarts one that is already up, and it never stops anything. Those
three are what keep "Faustus can use your app" from meaning "Faustus is in
charge of your app".
"""
from __future__ import annotations

import asyncio

import pytest

from src import plugin_runtime


@pytest.fixture
def world(monkeypatch):
    """One connected plugin with a launch profile, one connected without a
    way to start it, and one installed but never connected."""
    from src import plugins as plugins_mod

    rows = [
        {"id": "c-editor", "preset_id": "platos", "owner": "luis",
         "app_url": "http://127.0.0.1:5000", "ui_url": "http://127.0.0.1:5000",
         "launch_profile_id": "prof-editor", "values": {}},
        {"id": "c-notes", "preset_id": "writer", "owner": "luis",
         "app_url": "http://127.0.0.1:8766", "ui_url": None,
         "launch_profile_id": "", "values": {}},
    ]
    monkeypatch.setattr(plugin_runtime, "_connectors", lambda: list(rows))

    state = {"reachable": False, "launched": [], "opened": [], "waited": 0}

    async def fake_health(connector, plugin):
        state["waited"] += 1
        return {"reachable": state["reachable"], "detail": "test"}

    monkeypatch.setattr(plugin_runtime, "app_reachable", fake_health)
    monkeypatch.setattr(plugin_runtime, "POLL_S", 0.0)

    import src.launch_profiles as lp

    async def fake_launch(profile_id, **kwargs):
        state["launched"].append(profile_id)
        state["reachable"] = True          # the app comes up
        return {"launched": True, "pid": 4242}

    monkeypatch.setattr(lp, "launch", fake_launch)
    monkeypatch.setattr(lp, "open_desktop", lambda pid: state["opened"].append(pid) or {"ok": True})
    assert "platos" in plugins_mod.load_plugins()
    return state


# ── finding what was meant ──────────────────────────────────────────────────

def test_a_plugin_can_be_named_by_id_by_name_or_by_connection(world):
    for ref in ("platos", "Plato's Hoard", "c-editor"):
        found = plugin_runtime.resolve(ref)
        assert found["connector"]["id"] == "c-editor", ref


def test_an_unknown_name_comes_back_with_the_names_that_do_exist(world):
    found = plugin_runtime.resolve("spreadsheets")
    assert found["connector"] is None
    assert "installed plugins:" in found["reason"]
    assert "platos" in found["reason"]


def test_installed_but_not_connected_is_said_differently_from_unknown(world):
    found = plugin_runtime.resolve("dorian")   # ships, but no connector here
    assert found["plugin"] is not None and found["connector"] is None
    assert "not connected yet" in found["reason"]


# ── starting ────────────────────────────────────────────────────────────────

def test_an_app_that_is_already_running_is_adopted_not_restarted(world):
    world["reachable"] = True
    out = asyncio.run(plugin_runtime.ensure_running("platos"))
    assert out["ok"] and out["already_running"] is True
    assert world["launched"] == [], "starting a running app is how you lose someone's work"


def test_an_app_that_is_down_is_started_from_its_saved_profile(world):
    out = asyncio.run(plugin_runtime.ensure_running("platos"))
    assert out["ok"] and out.get("started") is True
    assert world["launched"] == ["prof-editor"]


def test_a_plugin_with_no_launch_profile_refuses_instead_of_guessing(world):
    """Guessing a command line for somebody else's application is how you
    run the wrong binary with the right name."""
    out = asyncio.run(plugin_runtime.ensure_running("writer"))
    assert out["ok"] is False
    assert "no launch profile" in out["reason"]
    assert world["launched"] == []


def test_an_app_that_never_answers_says_so_without_hanging(world, monkeypatch):
    import src.launch_profiles as lp

    async def launch_but_stay_down(profile_id, **kwargs):
        world["launched"].append(profile_id)
        return {"launched": True, "pid": 1}

    monkeypatch.setattr(lp, "launch", launch_but_stay_down)
    out = asyncio.run(plugin_runtime.ensure_running("platos", wait_s=0.05))
    assert out["ok"] is False
    assert out["started_process"] is True
    assert "did not answer" in out["reason"]


def test_a_launch_that_fails_reports_the_reason_it_was_given(world, monkeypatch):
    import src.launch_profiles as lp

    async def refuse(profile_id, **kwargs):
        return {"launched": False, "error": "executable not found: editor.exe"}

    monkeypatch.setattr(lp, "launch", refuse)
    out = asyncio.run(plugin_runtime.ensure_running("platos"))
    assert out["ok"] is False and "editor.exe" in out["reason"]


# ── showing ─────────────────────────────────────────────────────────────────

def test_showing_opens_the_window_the_profile_knows_about(world):
    out = plugin_runtime.show("platos")
    assert out["ok"] and out["shown"] == "desktop"
    assert world["opened"] == ["prof-editor"]


def test_an_app_with_no_window_and_no_page_says_what_it_is_instead(world):
    """Writer's Hoard's 8766 is a bridge, not a page. Offering to 'open' it
    would produce a blank tab and a puzzled user."""
    out = plugin_runtime.show("writer")
    assert out["ok"] is False
    assert "no window and no UI address" in out["reason"]


# ── the survey the agent reads ──────────────────────────────────────────────

def test_the_survey_says_what_can_be_done_with_each_plugin(world):
    rows = {r["plugin"]: r for r in asyncio.run(plugin_runtime.survey())}
    assert rows["platos"]["connected"] is True
    assert rows["platos"]["can_start"] is True and rows["platos"]["can_show"] is True
    assert rows["writer"]["connected"] is True
    assert rows["writer"]["can_start"] is False, "no profile, so nothing can start it"
    assert rows["dorian"]["connected"] is False
    # Installed-but-unconnected still shows what it would lend, which is how
    # a person decides whether connecting it is worth doing.
    assert rows["dorian"]["capabilities"] == ["credentials", "sessions"]


def test_the_survey_does_not_touch_the_network_unless_asked(world):
    world["waited"] = 0
    asyncio.run(plugin_runtime.survey())
    assert world["waited"] == 0, "listing what is installed is not a reason to probe"
    asyncio.run(plugin_runtime.survey(check=True))
    assert world["waited"] > 0


# ── the tools ───────────────────────────────────────────────────────────────

def test_the_list_tool_names_what_is_not_connected(world):
    from src.agent_tools.plugin_tools import PluginsListTool

    out = asyncio.run(PluginsListTool().execute("{}", {}))
    assert "NOT CONNECTED" in out["output"]
    assert "Plato's Hoard (platos)" in out["output"]


def test_the_app_tool_needs_to_be_told_which_plugin(world):
    from src.agent_tools.plugin_tools import PluginAppTool

    out = asyncio.run(PluginAppTool().execute("{}", {}))
    assert "say which plugin" in out["error"]


def test_the_app_tool_takes_a_bare_name_as_the_plugin(world):
    """A model in a hurry writes `plugin_app` with just the name."""
    from src.agent_tools.plugin_tools import PluginAppTool

    out = asyncio.run(PluginAppTool().execute("platos", {}))
    assert world["launched"] == ["prof-editor"], out


def test_start_and_show_does_both_and_reports_both(world):
    from src.agent_tools.plugin_tools import PluginAppTool

    out = asyncio.run(PluginAppTool().execute(
        '{"plugin": "platos", "action": "start_and_show"}', {}))
    assert world["launched"] == ["prof-editor"] and world["opened"] == ["prof-editor"]
    assert "started" in out["output"] and "opened" in out["output"]


def test_a_window_that_will_not_open_does_not_undo_a_successful_start(world, monkeypatch):
    import src.launch_profiles as lp

    monkeypatch.setattr(lp, "open_desktop", lambda pid: {"ok": False, "error": "no shell"})
    from src.agent_tools.plugin_tools import PluginAppTool

    out = asyncio.run(PluginAppTool().execute(
        '{"plugin": "platos", "action": "start_and_show"}', {}))
    assert "error" not in out, out
    assert world["launched"] == ["prof-editor"]


def test_there_is_no_way_to_stop_an_app_from_here(world, monkeypatch):
    """`launch_profiles.stop` exists and is a human's button. An agent that
    can stop an app it did not start can close the document you are in."""
    from src.agent_tools.plugin_tools import PluginAppTool
    import src.launch_profiles as lp

    stopped = []

    async def spy_stop(profile_id, **kwargs):
        stopped.append(profile_id)
        return {"ok": True}

    monkeypatch.setattr(lp, "stop", spy_stop)
    for asked in ("stop", "kill", "restart", "close"):
        out = asyncio.run(PluginAppTool().execute(
            '{"plugin": "platos", "action": "%s"}' % asked, {}))
        assert "unknown action" in out["error"], asked
    assert stopped == [], "no phrasing of the request reaches stop()"


# ── an app that came up somewhere else ─────────────────────────────────────

def _profile_on(monkeypatch, url):
    import src.launch_profiles as lp

    monkeypatch.setattr(lp, "get_profile", lambda pid: {"id": pid, "readiness": {"url": url}})

    async def answers(readiness):
        return readiness.get("url") == url

    monkeypatch.setattr(lp, "_check_ready_once", answers)


def test_an_app_up_at_another_address_is_named_not_waited_for(world, monkeypatch):
    """Seen live: the profile started the app on :5179, the connection
    pointed at :5178, and the call waited 30 s to say "it may still be coming
    up" about an app that was up. The disagreement is the answer."""
    import src.launch_profiles as lp

    async def launch_but_elsewhere(profile_id, **kwargs):
        world["launched"].append(profile_id)
        return {"launched": True, "pid": 1}

    monkeypatch.setattr(lp, "launch", launch_but_elsewhere)
    _profile_on(monkeypatch, "http://127.0.0.1:5001/api/health")

    out = asyncio.run(plugin_runtime.ensure_running("platos", wait_s=30))

    assert out["ok"] is False and out["started_process"] is True
    assert out["answering_at"] == "http://127.0.0.1:5001/api/health"
    assert "127.0.0.1:5000" in out["reason"] and "disagree" in out["reason"]
    assert world["waited"] <= 2, "it must not sit out the whole wait"


def test_a_profile_on_the_connection_address_is_not_a_disagreement(world, monkeypatch):
    # localhost and 127.0.0.1, with a health path, are the same place.
    _profile_on(monkeypatch, "http://localhost:5000/api/health")

    out = asyncio.run(plugin_runtime.ensure_running("platos"))

    assert out["ok"] is True and out.get("started") is True


def test_the_list_opens_with_which_apps_run_and_which_do_not(monkeypatch):
    """«¿Qué aplicaciones mías están abiertas?» with 23 apps: the model listed
    the first four as the open ones. The answer now comes first."""
    from src import plugin_runtime
    from src.agent_tools.plugin_tools import PluginsListTool

    rows = [
        {"plugin": "a", "name": "Alpha", "capabilities": [], "connected": True, "app": {"reachable": True},
         "can_start": True, "can_show": True},
        {"plugin": "b", "name": "Beta", "capabilities": [], "connected": True, "app": {"reachable": False},
         "can_start": True, "can_show": True},
        {"plugin": "c", "name": "Gamma", "capabilities": [], "connected": False, "app": None,
         "can_start": False, "can_show": False},
    ]

    async def fake_survey(owner=None, check=False):
        return rows

    monkeypatch.setattr(plugin_runtime, "survey", fake_survey)
    out = asyncio.run(PluginsListTool().execute('{"check": true}', {}))["output"].splitlines()
    assert out[0] == "Running now (1): Alpha"
    assert out[1] == "Not running (1): Beta"
    assert out[2] == "Not connected (1): Gamma"
