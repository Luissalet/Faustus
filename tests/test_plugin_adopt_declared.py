"""Adopting an application that declares itself (connectors.adopt_declared_app).

Discovery already recognised such an app from its own `faustus-plugin.json`,
but pressing Add ended in "Unknown preset": a connector is built from a
preset, and nothing turned the declaration into one. Seen live with an app
Faustus ships nothing for, on port 8814.
"""
from __future__ import annotations

import json

import pytest

from src import connectors, plugins


NOVEL = {
    "schema": 1,
    "id": "abacus",
    "name": "Abacus",
    "purpose": "Keep and query a household abacus.",
    "capabilities": ["accounts"],
    "placeholders": ["ABACUS_DIR", "APP_URL"],
    "defaults": {"APP_URL": "http://127.0.0.1:8790"},
    "app": {
        "url_default": "http://127.0.0.1:8790",
        "ui_url": "{APP_URL}",
        "health": {"path": "/api/health", "expect": {"service": "abacus"}},
        "identify": {"service": ["abacus"]},
    },
    "mcp": {"command": "node", "args": ["{ABACUS_DIR}/mcp.js"]},
}


@pytest.fixture
def app_dir(tmp_path):
    root = tmp_path / "abacus-app"
    root.mkdir()
    (root / plugins.APP_MANIFEST_NAME).write_text(json.dumps(NOVEL), encoding="utf-8")
    (root / "mcp.js").write_text("// bridge", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "user_dir", lambda: str(tmp_path / "installed"))
    connectors.reload_presets()
    yield
    monkeypatch.undo()
    connectors.reload_presets()


def test_adopting_a_declared_app_installs_it_and_makes_a_preset(app_dir):
    assert connectors.get_preset("abacus") is None
    out = connectors.adopt_declared_app(str(app_dir), "abacus")
    assert out["ok"] and out["installed"] is True
    preset = connectors.get_preset("abacus")
    assert preset is not None and preset.name == "Abacus"
    resolved = connectors.resolve_preset_values(
        preset, {"APP_URL": "http://127.0.0.1:8790", "ABACUS_DIR": str(app_dir)})
    assert resolved["ok"], resolved


def test_a_known_id_is_never_replaced_by_a_declaration(app_dir):
    shipped = connectors.get_preset("jobhunter")
    assert shipped is not None
    out = connectors.adopt_declared_app(str(app_dir), "jobhunter")
    assert out == {"ok": True, "id": "jobhunter", "installed": False}
    assert connectors.get_preset("jobhunter") is shipped


def test_a_declaration_that_changed_id_since_the_scan_is_refused(app_dir):
    out = connectors.adopt_declared_app(str(app_dir), "something-else")
    assert out["ok"] is False and "scan again" in out["reason"]
    assert connectors.get_preset("something-else") is None


def test_no_manifest_says_so(tmp_path):
    out = connectors.adopt_declared_app(str(tmp_path), "abacus")
    assert out["ok"] is False and plugins.APP_MANIFEST_NAME in out["reason"]
