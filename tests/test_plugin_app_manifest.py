"""An application that declares itself (src/plugins.py, connector_discovery).

The point of the whole plugin layer: adding Faustus support to one of your
own projects should not require a patch to Faustus. The author of an
application knows what it exposes; they say so in a `faustus-plugin.json` in
their own repository, and an app running with one of those beside it is
connectable on sight — even though Faustus ships no knowledge of it at all.

These tests use an app Faustus has never heard of, deliberately. Using a
known one would prove nothing: the answer could be coming from the shipped
manifest. (It was called `ledger` until a real Ledger's Hoard shipped.)
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src import plugins


NOVEL = {
    "schema": 1,
    "id": "abacus",
    "name": "Abacus",
    "purpose": "Keep and query a household abacus of accounts.",
    "capabilities": ["accounts", "entries", "reports"],
    "placeholders": ["ABACUS_DIR", "APP_URL"],
    "defaults": {"APP_URL": "http://127.0.0.1:8790"},
    "app": {
        "url_default": "http://127.0.0.1:8790",
        "ui_url": "{APP_URL}",
        "health": {"path": "/api/health", "expect": {"service": "abacus"}},
        "identify": {"service": ["abacus"], "title": ["abacus"]},
        "launch_hint": {"kind": "process", "executable": "node",
                        "argv": ["server.js"], "cwd": "{ABACUS_DIR}"},
    },
    "mcp": {"command": "node", "args": ["{ABACUS_DIR}/mcp.js"]},
}


@pytest.fixture
def app_dir(tmp_path):
    root = tmp_path / "abacus-app"
    root.mkdir()
    (root / plugins.APP_MANIFEST_NAME).write_text(
        json.dumps(NOVEL, indent=2), encoding="utf-8")
    (root / "mcp.js").write_text("// bridge", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "user_dir", lambda: str(tmp_path / "installed"))
    plugins.reset_cache()
    yield
    plugins.reset_cache()


def test_an_app_can_say_what_it_offers_from_its_own_repository(app_dir):
    declared = plugins.read_app_manifest(str(app_dir))
    assert declared is not None
    assert declared.id == "abacus" and declared.name == "Abacus"
    assert declared.source == "app"
    assert declared.capabilities == ["accounts", "entries", "reports"]
    assert "abacus" not in plugins.load_plugins(), "reading is not installing"


def test_a_directory_with_nothing_to_say_is_not_an_error(tmp_path):
    assert plugins.read_app_manifest(str(tmp_path)) is None
    assert plugins.read_app_manifest("") is None
    assert plugins.read_app_manifest(r"X:\nowhere") is None


def test_a_malformed_declaration_is_ignored_not_raised(tmp_path):
    """It is a file in somebody else's repository and Faustus is reading it
    uninvited. Being wrong about it must cost nothing."""
    root = tmp_path / "broken-app"
    root.mkdir()
    (root / plugins.APP_MANIFEST_NAME).write_text("{ nope", encoding="utf-8")
    assert plugins.read_app_manifest(str(root)) is None


def test_adopting_it_installs_a_snapshot_the_app_cannot_change_later(app_dir):
    """Not a link: an app that changes what it offers in a later version is
    a change the user should see and accept, not one that rewrites a live
    connection underneath them."""
    out = plugins.install_from_dir(str(app_dir))
    assert out["ok"] and out["id"] == "abacus"
    assert "abacus" in plugins.load_plugins()
    assert plugins.get("abacus").source == "user"

    # The app changes its mind; the installed copy does not move.
    changed = dict(NOVEL, name="Abacus Pro")
    (app_dir / plugins.APP_MANIFEST_NAME).write_text(
        json.dumps(changed), encoding="utf-8")
    plugins.reset_cache()
    assert plugins.get("abacus").name == "Abacus"
    assert plugins.read_app_manifest(str(app_dir)).name == "Abacus Pro"


def test_adopting_a_directory_with_no_manifest_says_so(tmp_path):
    out = plugins.install_from_dir(str(tmp_path))
    assert out["ok"] is False and plugins.APP_MANIFEST_NAME in out["reason"]


def test_an_unknown_app_is_offered_because_it_declared_itself(app_dir, monkeypatch):
    """The whole chain, on an application Faustus ships nothing for: found
    listening, not matched by any shipped fingerprint, recognised anyway."""
    from src import connector_discovery as disco

    async def fake_probe(url, health_path=None, tokens=None):
        return {"health": {"service": "something-nobody-knows"},
                "title": "Some App", "latency_ms": 3}

    monkeypatch.setattr(disco, "probe", fake_probe)
    port = disco.ListeningPort(port=8790, pid=99, process="node.exe", cwd=str(app_dir))

    found = asyncio.run(disco.discover(ports=[port]))
    assert len(found) == 1
    candidate = found[0]
    assert candidate.preset_id == "abacus"
    assert candidate.preset_name == "Abacus"
    assert candidate.declares_itself.endswith(plugins.APP_MANIFEST_NAME)
    # And the form is prefilled from what was found, not typed by hand.
    assert candidate.values["APP_URL"] == "http://127.0.0.1:8790"
    assert candidate.values["ABACUS_DIR"] == str(app_dir)


def test_an_unknown_app_with_nothing_to_declare_stays_unknown(tmp_path, monkeypatch):
    from src import connector_discovery as disco

    async def fake_probe(url, health_path=None, tokens=None):
        return {"health": None, "title": "Some App", "latency_ms": 3}

    monkeypatch.setattr(disco, "probe", fake_probe)
    port = disco.ListeningPort(port=8791, pid=1, process="node.exe", cwd=str(tmp_path))
    found = asyncio.run(disco.discover(ports=[port]))
    assert len(found) == 1
    assert found[0].preset_id is None and found[0].declares_itself is None


def test_a_shipped_plugin_still_wins_over_a_stray_declaration(app_dir, monkeypatch):
    """`declares_itself` is the fallback for an app nothing knows about, not
    a way for any directory to claim to be a plugin Faustus already ships."""
    from src import connector_discovery as disco

    async def fake_probe(url, health_path=None, tokens=None):
        return {"health": {"service": "jubhunters-hoard"}, "title": "", "latency_ms": 1}

    monkeypatch.setattr(disco, "probe", fake_probe)
    port = disco.ListeningPort(port=5178, pid=2, process="node.exe", cwd=str(app_dir))
    found = asyncio.run(disco.discover(ports=[port]))
    assert found[0].preset_id == "jobhunter"
    assert found[0].declares_itself is None
