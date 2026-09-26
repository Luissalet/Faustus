"""One manifest per plugin, and what that buys (src/plugins.py).

A plugin here is somebody's standalone application — Jobhunter's Hoard is a
job-search app you use on its own — that Faustus can connect to. The manifest
is Faustus's side of that contract: where the app lives, how to recognise it,
what tools it lends. It is not a claim of ownership over the app.

The regression that motivated the module is the one asserted first: a plugin
Faustus cannot RECOGNISE is a plugin you have to configure by hand even while
its app is running in front of you. Three of the five shipped plugins were in
that state, because the fingerprint lived in a different file from the plugin
and the second file stopped being updated after the second plugin.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from src import plugins


# ── what ships ──────────────────────────────────────────────────────────────

def test_every_shipped_plugin_loads_with_no_errors():
    loaded = plugins.load_all()
    assert loaded.errors == [], loaded.errors
    assert set(loaded.plugins) >= {"jobhunter", "writer", "dorian", "gepetto", "platos"}


def test_every_shipped_plugin_can_be_recognised_when_its_app_is_running():
    """The bug this module exists for. An app that answers on a port is only
    offered as a one-click connection if something can tell which plugin it
    is; a plugin with no `identify` can never be that."""
    unrecognisable = sorted(
        pid for pid, plugin in plugins.load_plugins().items() if not plugin.identify
    )
    assert unrecognisable == [], (
        f"these plugins could never be found by a scan: {unrecognisable}")


def test_a_shipped_manifest_never_carries_one_persons_home_directory():
    """A manifest travels — it ships in the repo, and it can travel inside
    the app it describes. A default expanded on the machine that wrote it
    (``C:\\Users\\someone\\AppData\\...``) is wrong everywhere else and is a
    personal detail in a public file. Write ``%APPDATA%/...``; the loader
    expands it on the machine that reads it."""
    home_like = re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/]|/home/|/Users/)", re.IGNORECASE)
    offenders = []
    root = plugins.builtin_dir()
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry, plugins.MANIFEST_NAME)
        if not os.path.isfile(path):
            continue
        raw = open(path, encoding="utf-8").read()
        for hit in home_like.findall(raw):
            offenders.append(f"{entry}: {hit}")
    assert offenders == [], offenders


def test_a_default_naming_an_environment_variable_is_expanded_once(monkeypatch, tmp_path):
    # Windows always has APPDATA; elsewhere the manifest's %APPDATA% expands
    # only when the variable is set, so the test sets it.
    import os as _os
    if not _os.environ.get("APPDATA"):
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    plugin = plugins.get("writer")
    token = plugin.defaults.get("TOKEN_FILE", "")
    assert "%APPDATA%" not in token, "the manifest keeps the variable; the loader resolves it"
    assert token.endswith(os.path.join("writers-hoard", "aibridge", "token"))


def test_whether_an_app_has_a_ui_is_declared_by_that_app_not_by_a_special_case(tmp_path):
    """`resolve_preset_values` used to answer this with
    `if preset.id == "jobhunter"`. One plugin's detail, sitting in the code
    path every plugin goes through — so no plugin added afterwards could
    ever have a UI, whatever its manifest said.

    Asserted on a plugin with an invented id, which is the whole point: the
    answer has to come from the manifest, and an id nothing was written for
    is the only way to prove nothing was written for it.
    """
    from src.connectors import resolve_preset_values

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "bridge.js").write_text("", encoding="utf-8")
    values = {"SAMPLE_DIR": str(app_dir), "APP_URL": "http://127.0.0.1:9931"}

    with_ui = plugins.parse_manifest(_minimal(app={"ui_url": "{APP_URL}"})).to_preset()
    assert resolve_preset_values(with_ui, values)["ui_url"] == "http://127.0.0.1:9931"

    headless = plugins.parse_manifest(_minimal(app={"ui_url": None})).to_preset()
    assert resolve_preset_values(headless, values)["ui_url"] is None

    # And the two that ship: Jobhunter is a page, Writer's 8766 is an API.
    assert plugins.get("jobhunter").ui_url == "{APP_URL}"
    assert plugins.get("writer").ui_url is None


# ── the manifest is checked, and a bad one is survivable ────────────────────

def _minimal(**over):
    data = {
        "schema": 1,
        "id": "sample",
        "name": "Sample",
        "mcp": {"command": "node", "args": ["{SAMPLE_DIR}/bridge.js"]},
    }
    data.update(over)
    return data


def test_a_minimal_manifest_is_enough():
    plugin = plugins.parse_manifest(_minimal())
    assert plugin.id == "sample" and plugin.command == "node"
    assert plugin.identify == {} and plugin.capabilities == []


@pytest.mark.parametrize("data, fragment", [
    ({"id": "x", "name": "X", "mcp": {"command": "node"}}, "no 'schema'"),
    (_minimal(schema=999), "understands up to"),
    (_minimal(id=""), "no 'id'"),
    (_minimal(id="not valid"), "alphanumeric"),
    (_minimal(name=""), "no 'name'"),
    (_minimal(mcp={"command": ""}), "'mcp.command' is required"),
    (_minimal(mcp={"command": "node", "transport": "http"}), "must be 'stdio'"),
    (_minimal(capabilities="tools"), "'capabilities' must be a list"),
    (_minimal(app={"health": {"path": "/x", "expct": {}}}), "unknown key"),
    (_minimal(provides={"skils": []}), "unknown key"),
    (_minimal(wat=1), "unknown key"),
])
def test_a_manifest_is_refused_with_a_reason_a_person_can_act_on(data, fragment):
    with pytest.raises(plugins.ManifestError) as exc:
        plugins.parse_manifest(data)
    assert fragment in str(exc.value), str(exc.value)


def test_extension_keys_are_skipped_not_refused():
    """A manifest is read by more than Faustus (the family's desktop hub, a
    launcher): what those readers need lives under ``x-`` keys and must not
    make the manifest fail here. A plain unknown key still does."""
    plugin = plugins.parse_manifest(_minimal(**{"x-tools": ["a", "b"], "x_desktop": True},
                                             app={"health": {"path": "/x", "x-probe": 3}},
                                             provides={"x-recipes": []}))
    assert plugin.id == "sample" and plugin.health_path == "/x"
    with pytest.raises(plugins.ManifestError):
        plugins.parse_manifest(_minimal(tools=["a"]))


def test_one_bad_manifest_does_not_take_the_others_with_it(tmp_path, monkeypatch):
    """The alternative — one malformed third-party file and Faustus has no
    connectors at all — is not a trade worth making."""
    root = tmp_path / "plugins"
    (root / "good").mkdir(parents=True)
    (root / "good" / "plugin.json").write_text(
        json.dumps(_minimal(id="good", name="Good")), encoding="utf-8")
    (root / "broken").mkdir()
    (root / "broken" / "plugin.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(plugins, "builtin_dir", lambda: str(root))
    monkeypatch.setattr(plugins, "user_dir", lambda: "")

    loaded = plugins.load_all()
    assert set(loaded.plugins) == {"good"}
    assert len(loaded.errors) == 1 and "broken" in loaded.errors[0]["path"]


def test_an_id_that_disagrees_with_its_folder_is_refused(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    (root / "folder-name").mkdir(parents=True)
    (root / "folder-name" / "plugin.json").write_text(
        json.dumps(_minimal(id="other-name")), encoding="utf-8")
    monkeypatch.setattr(plugins, "builtin_dir", lambda: str(root))
    monkeypatch.setattr(plugins, "user_dir", lambda: "")

    loaded = plugins.load_all()
    assert loaded.plugins == {}
    assert "does not match its folder name" in loaded.errors[0]["reason"]


def test_a_user_manifest_replaces_a_shipped_one_and_says_so(tmp_path, monkeypatch):
    """How you patch a shipped plugin without forking Faustus — and the
    replacement is reported, because a silent one is a debugging session."""
    shipped = tmp_path / "builtin"
    (shipped / "sample").mkdir(parents=True)
    (shipped / "sample" / "plugin.json").write_text(
        json.dumps(_minimal(name="Shipped")), encoding="utf-8")
    mine = tmp_path / "user"
    (mine / "sample").mkdir(parents=True)
    (mine / "sample" / "plugin.json").write_text(
        json.dumps(_minimal(name="Mine")), encoding="utf-8")
    monkeypatch.setattr(plugins, "builtin_dir", lambda: str(shipped))
    monkeypatch.setattr(plugins, "user_dir", lambda: str(mine))

    loaded = plugins.load_all()
    assert loaded.plugins["sample"].name == "Mine"
    assert loaded.plugins["sample"].source == "user"
    assert loaded.shadowed and loaded.shadowed[0]["id"] == "sample"


# ── the views the rest of the code reads ────────────────────────────────────

def test_presets_are_a_view_of_the_manifests():
    from src.connectors import PRESETS

    assert set(PRESETS) == set(plugins.load_plugins())
    for pid, plugin in plugins.load_plugins().items():
        preset = PRESETS[pid]
        assert preset.name == plugin.name
        assert preset.command == plugin.command
        assert preset.args == plugin.args
        assert preset.launch_profile_hint == plugin.launch_hint


def test_every_plugin_is_matched_by_what_its_own_app_actually_answers():
    from src.connector_discovery import match_preset

    assert match_preset({"service": "jubhunters-hoard"}, "") == "jobhunter"
    assert match_preset({"service": "writers-hoard-ai-bridge"}, "") == "writer"
    # The three that no fingerprint knew about before this module:
    assert match_preset({"application": "sculptors-hoard"}, "") == "gepetto"
    assert match_preset({"mode": "local"}, "") == "dorian"
    assert match_preset(None, "Plato's Hoard — editor") == "platos"
    assert match_preset({"service": "something-else"}, "My app") is None


def test_the_scan_asks_every_health_path_a_plugin_declares():
    from src.connector_discovery import health_paths

    paths = health_paths()
    assert paths[0] == "/api/health", "the common case is asked first"
    assert "/api/session" in paths, "Dorian's Hoard answers there and nowhere else"
    assert len(paths) == len(set(paths)), paths


def test_reset_cache_picks_up_a_newly_installed_plugin(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    monkeypatch.setattr(plugins, "builtin_dir", lambda: str(root))
    monkeypatch.setattr(plugins, "user_dir", lambda: "")
    plugins.reset_cache()
    assert plugins.load_plugins() == {}

    (root / "late").mkdir(parents=True)
    (root / "late" / "plugin.json").write_text(
        json.dumps(_minimal(id="late", name="Late")), encoding="utf-8")
    assert plugins.load_plugins() == {}, "still the cached answer"
    plugins.reset_cache()
    assert set(plugins.load_plugins()) == {"late"}


@pytest.fixture(autouse=True)
def _restore_cache():
    yield
    plugins.reset_cache()
