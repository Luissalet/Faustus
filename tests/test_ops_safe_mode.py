"""OPS-05 / QA-46 — safe mode: automatic activation after repeated failed
boots or FAUSTUS_SAFE_MODE=1, MCP quarantine reusing mcp_manager's own
`needs_auth`/`error` states (no second disable list — see `_add_to_disabled_tools`
in `src/safe_mode.py`), one-at-a-time reactivation, and the core never gated.
"""
from __future__ import annotations

import pytest

from src import safe_mode


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """A private settings.json per test — safe_mode persists its state there
    (see `src/safe_mode.py::_set`), and this suite must never touch the real
    one."""
    import src.settings as settings_module
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_module._invalidate_caches()
    monkeypatch.delenv("FAUSTUS_SAFE_MODE", raising=False)
    safe_mode._active_cache = None
    safe_mode._reason_cache = ""
    yield
    settings_module._invalidate_caches()
    safe_mode._active_cache = None
    safe_mode._reason_cache = ""


def test_env_var_forces_safe_mode():
    import os
    os.environ["FAUSTUS_SAFE_MODE"] = "1"
    try:
        should, why = safe_mode.should_activate()
        assert should
        assert "FAUSTUS_SAFE_MODE" in why
    finally:
        del os.environ["FAUSTUS_SAFE_MODE"]


def test_two_consecutive_unfinished_boots_trigger_safe_mode_automatically():
    """The literal OPS-05 requirement: 'tras dos fallos de arranque
    consecutivos' — no env var involved at all."""
    assert safe_mode.mark_boot_started() == 0    # boot 1: clean start
    assert safe_mode.mark_boot_started() == 1    # boot 2: boot 1 never finished
    assert safe_mode.mark_boot_started() == 2    # boot 3: boot 2 never finished either
    should, why = safe_mode.should_activate()
    assert should and "2" in why


def test_a_completed_boot_resets_the_failure_counter():
    safe_mode.mark_boot_started()
    safe_mode.mark_boot_started()
    safe_mode.mark_boot_completed()
    assert safe_mode.consecutive_boot_failures() == 0
    should, _ = safe_mode.should_activate()
    assert not should


def test_active_safe_mode_holds_back_every_subsystem_but_none_gate_core():
    safe_mode.activate("test")
    disabled = safe_mode.disabled_subsystems()
    assert all(disabled.values())
    assert set(disabled) == set(safe_mode.SUBSYSTEMS)
    # Core (chat/files/settings) has no on/off switch in this module at all —
    # the absence of a "core" key here IS the guarantee it is never gated.
    assert "core" not in disabled
    assert safe_mode.status()["core_available"] is True


def test_reactivation_is_one_subsystem_at_a_time():
    safe_mode.activate("test")
    safe_mode.reactivate_subsystem("scheduled_tasks")
    disabled = safe_mode.disabled_subsystems()
    assert disabled["scheduled_tasks"] is False
    assert disabled["mcp_external"] is True
    assert disabled["plugins_third_party"] is True


def test_reactivate_subsystem_rejects_an_unknown_name():
    safe_mode.activate("test")
    with pytest.raises(ValueError):
        safe_mode.reactivate_subsystem("not-a-real-subsystem")


def test_a_repeatedly_crashing_mcp_server_is_quarantined_automatically():
    """QA-46's literal scenario: 'extension se cuelga al iniciar' — repeated
    `error`/`needs_auth` connection attempts, no manual step by the user.
    """
    from src.settings import get_setting

    for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD - 1):
        result = safe_mode.record_mcp_connection("flaky-mcp", "error")
        assert not result["quarantined"]
    final = safe_mode.record_mcp_connection("flaky-mcp", "error")
    assert final["quarantined"] and final["newly_quarantined"]
    assert "flaky-mcp" in safe_mode.quarantined_servers()
    # Reuses the EXISTING disabled_tools authority — not a second store.
    assert "flaky-mcp" in (get_setting("disabled_tools", []) or [])


def test_needs_auth_counts_the_same_as_error():
    for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
        result = safe_mode.record_mcp_connection("perm-hungry-mcp", "needs_auth")
    assert result["quarantined"]


def test_a_healthy_connection_resets_that_servers_failure_count():
    safe_mode.record_mcp_connection("recovering-mcp", "error")
    safe_mode.record_mcp_connection("recovering-mcp", "error")
    safe_mode.record_mcp_connection("recovering-mcp", "connected")
    result = safe_mode.record_mcp_connection("recovering-mcp", "error")
    assert result["fail_count"] == 1, "the success in between must have cleared the count"
    assert not result["quarantined"]


def test_quarantine_is_scoped_to_the_one_server_that_failed():
    for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
        safe_mode.record_mcp_connection("bad-server", "error")
    healthy = safe_mode.record_mcp_connection("good-server", "connected")
    assert not healthy["quarantined"]
    assert "good-server" not in safe_mode.quarantined_servers()


def test_manual_reactivation_of_one_quarantined_server_only_affects_that_one():
    from src.settings import get_setting

    for server in ("bad-a", "bad-b"):
        for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
            safe_mode.record_mcp_connection(server, "error")
    safe_mode.reactivate_mcp_server("bad-a")
    disabled = get_setting("disabled_tools", []) or []
    assert "bad-a" not in disabled
    assert "bad-b" in disabled
    assert "bad-a" not in safe_mode.quarantined_servers()
    assert "bad-b" in safe_mode.quarantined_servers()


# ── proof: without safe_mode's threshold, a single crash would not be enough ─

def test_below_threshold_the_server_is_not_yet_quarantined():
    """Demonstrates the threshold is real, not a rubber stamp: one failure
    alone must never disable a server that might just be starting up slowly.
    """
    result = safe_mode.record_mcp_connection("just-slow", "error")
    assert not result["quarantined"]
    assert result["fail_count"] == 1 < safe_mode.MCP_QUARANTINE_THRESHOLD
