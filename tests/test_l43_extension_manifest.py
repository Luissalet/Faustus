"""L43 · TOOL-04 — src/extension_manifest.py (new): a versioned manifest per
installed MCP server/plugin, permission diffing, and quarantine on an
unapproved new permission (docs/spec/v2/backlog.json TOOL-04).
"""
from __future__ import annotations

import pytest

from src import extension_manifest as em
from src import settings as settings_mod


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    yield
    settings_mod._invalidate_caches()


def test_command_hash_changes_with_command_or_args():
    a = em.command_hash("npx", ["-y", "some-server"])
    b = em.command_hash("npx", ["-y", "some-server", "--extra"])
    c = em.command_hash("npx", ["-y", "some-server"])
    assert a == c
    assert a != b


def test_diff_permissions_reports_added_and_removed():
    old = {"network": True, "files": False, "secrets": False}
    new = {"network": True, "files": True, "secrets": False}
    assert em.diff_permissions(old, new) == {"added": ["files"], "removed": []}
    assert em.diff_permissions(new, old) == {"added": [], "removed": ["files"]}
    # No prior manifest: every True key in `new` is reported as added, so
    # the UI still has something to show before a first install.
    assert em.diff_permissions(None, new) == {"added": ["files", "network"], "removed": []}


def test_first_install_is_recorded_and_never_quarantined(store):
    out = em.record_install_or_update(
        "srv1", name="Some MCP", version="1.0.0", command="npx", args=["-y", "pkg"],
        dependencies=["pkg"], permissions={"network": True},
    )
    assert out["quarantined"] is False
    assert out["manifest"]["permissions"] == {"network": True, "files": False, "secrets": False}
    assert out["manifest"]["pending_approval"] is False
    assert em.get_manifest("srv1")["version"] == "1.0.0"


def test_update_with_a_new_permission_quarantines_pending_approval(store):
    em.record_install_or_update("srv1", name="Some MCP", version="1.0.0", permissions={"network": True})
    out = em.record_install_or_update(
        "srv1", name="Some MCP", version="1.1.0", permissions={"network": True, "files": True},
    )
    assert out["quarantined"] is True
    assert out["diff"]["added"] == ["files"]
    assert out["manifest"]["pending_approval"] is True
    assert em.is_quarantined_for_permissions("srv1") is True
    disabled = settings_mod.get_setting("disabled_tools", [])
    assert "srv1" in disabled


def test_update_that_only_drops_a_permission_is_never_quarantined(store):
    em.record_install_or_update("srv1", permissions={"network": True, "files": True})
    out = em.record_install_or_update("srv1", permissions={"network": True, "files": False})
    assert out["quarantined"] is False
    assert em.is_quarantined_for_permissions("srv1") is False


def test_approve_clears_pending_and_lifts_quarantine(store):
    em.record_install_or_update("srv1", permissions={"network": True})
    em.record_install_or_update("srv1", permissions={"network": True, "secrets": True})
    assert em.is_quarantined_for_permissions("srv1") is True
    approved = em.approve_new_permissions("srv1")
    assert approved["pending_approval"] is False
    assert em.is_quarantined_for_permissions("srv1") is False
    assert "srv1" not in (settings_mod.get_setting("disabled_tools", []) or [])


def test_approve_does_not_disturb_an_unrelated_disabled_tools_entry(store):
    """The quarantine list is shared with safe_mode's own MCP-failure
    quarantine and with plain tool-disable entries — un-quarantining one
    server must not touch anyone else's entry in the same list."""
    settings_mod.save_settings({"disabled_tools": ["some_other_server", "a_plain_tool_name"]})
    em.record_install_or_update("srv1", permissions={"network": True})
    em.record_install_or_update("srv1", permissions={"network": True, "files": True})
    em.approve_new_permissions("srv1")
    remaining = settings_mod.get_setting("disabled_tools", [])
    assert set(remaining) == {"some_other_server", "a_plain_tool_name"}


def test_approve_with_no_manifest_raises(store):
    with pytest.raises(ValueError):
        em.approve_new_permissions("never-installed")


def test_suggested_permissions_is_a_heuristic_starting_point_only():
    s = em.suggested_permissions(transport="sse")
    assert s["network"] is True
    s2 = em.suggested_permissions(command="npx", args=["-y", "@some/server", "/home/user/data"])
    assert s2["network"] is True   # npx pulls a package over the network
    assert s2["files"] is True     # a path-looking argument
    s3 = em.suggested_permissions(command="python3", args=["server.py"], env={"API_TOKEN": "x"})
    assert s3["secrets"] is True
    assert s3["network"] is False
