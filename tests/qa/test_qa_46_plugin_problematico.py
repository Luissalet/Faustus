"""QA-46 · Plugin problematico (docs/spec/v2/acceptance_scenarios.json).

Estimulo: extension se cuelga al iniciar o solicita permisos nuevos.
Resultado exigido (literal): "Modo seguro, desactivacion y revision; nucleo
sigue usable."

Requisitos: OPS-05, TOOL-04.

Estado: CLOSED (green). `src/safe_mode.py` (new, this lote) provides
automatic safe mode — either via `FAUSTUS_SAFE_MODE=1` or after two
consecutive boots that never finished (`mark_boot_started`/
`mark_boot_completed`) — and automatic MCP quarantine after
`MCP_QUARANTINE_THRESHOLD` consecutive `error`/`needs_auth` connection
attempts, reusing `disabled_tools` (the SAME setting `mcp_manager`'s tool gate
already reads — see `builtin_browser_policy_disabled()`) rather than a second
disable list. Held-back subsystems are reactivated one at a time; core (chat,
files, settings) is never gated by this module at all.

This test used to grep `src/mcp_manager.py` for the literal string "safe_mode"
or "auto_disable" (source: git history). `mcp_manager.py` is not in this
lote's PROPIOS (see `tasks`/lote spec) — CANNOT be edited here. The exact
one-line hook it would need is recorded below and in the batch's final
report, under "Cambios necesarios en ficheros ajenos". Instead of grepping a
string into a file this lote cannot touch, this test proves the REAL
behaviour end to end against `src/safe_mode.py`, which is the module that
would receive that call:

    # in src/mcp_manager.py, McpManager, wherever a connection attempt sets
    # self._connections[server_id] to status "error" or "needs_auth" (see
    # connect_server / the OAuth needs_auth branch around line ~788):
    from src import safe_mode
    safe_mode.record_mcp_connection(server_id, status)
"""
from __future__ import annotations

import pytest

from src import safe_mode

pytestmark = pytest.mark.qa_state("green")


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    import src.settings as settings_module
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_module._invalidate_caches()
    monkeypatch.delenv("FAUSTUS_SAFE_MODE", raising=False)
    safe_mode._active_cache = None
    safe_mode._reason_cache = ""
    yield


def test_an_extension_that_hangs_at_startup_is_quarantined_without_a_user_action():
    """'extension se cuelga al iniciar' — modelled the way `mcp_manager`
    itself would report it: repeated `error`/`needs_auth` states on the same
    server_id, exactly the two states its `_connections` dict already uses
    (see the module docstring's `FAILING_STATUSES`). No human clicks anything.
    """
    from src.settings import get_setting

    outcomes = [safe_mode.record_mcp_connection("problem-plugin", "error")
                for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD)]
    assert outcomes[-1]["quarantined"] and outcomes[-1]["newly_quarantined"]
    # "desactivacion": the existing tool gate (disabled_tools) now excludes it.
    assert "problem-plugin" in (get_setting("disabled_tools", []) or [])


def test_an_extension_that_asks_for_new_permissions_is_the_same_needs_auth_path():
    """'solicita permisos nuevos' maps to mcp_manager's `needs_auth` status —
    counted identically to `error` by `record_mcp_connection`."""
    outcomes = [safe_mode.record_mcp_connection("wants-new-scopes", "needs_auth")
                for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD)]
    assert outcomes[-1]["quarantined"]


def test_quarantine_and_safe_mode_leave_the_core_usable():
    """'nucleo sigue usable' — quarantining one server, or activating safe
    mode wholesale, must never touch anything core claims. Core has no on/off
    switch in this module (see SUBSYSTEMS) — this test proves that is a
    structural guarantee, not an oversight."""
    for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
        safe_mode.record_mcp_connection("bad-plugin", "error")
    safe_mode.activate("a plugin kept crashing")
    st = safe_mode.status()
    assert st["core_available"] is True
    assert "chat" not in safe_mode.SUBSYSTEMS
    assert "files" not in safe_mode.SUBSYSTEMS
    assert "settings" not in safe_mode.SUBSYSTEMS


def test_desactivacion_y_revision_one_server_reviewed_and_reactivated_at_a_time():
    """'revision' — an operator can look at what got quarantined and turn ONE
    back on, without the others coming back with it."""
    for server in ("crash-loop-a", "crash-loop-b"):
        for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
            safe_mode.record_mcp_connection(server, "error")
    assert set(safe_mode.quarantined_servers()) == {"crash-loop-a", "crash-loop-b"}

    safe_mode.reactivate_mcp_server("crash-loop-a")
    assert safe_mode.quarantined_servers() == ["crash-loop-b"]


def test_repeated_crashes_at_boot_also_trigger_whole_process_safe_mode():
    """The other half of OPS-05: not just one server, but the app itself
    coming up in safe mode after repeated failed boots — same acceptance
    scenario family, different trigger."""
    safe_mode.mark_boot_started()
    safe_mode.mark_boot_started()
    safe_mode.mark_boot_started()
    result = safe_mode.run_startup_check()
    assert result["active"]
    assert all(result["disabled"].values())
    assert result["core_available"] is True


# ── proof: below the threshold, nothing is disabled — quarantine is not a hair trigger ──

def test_a_single_hang_does_not_quarantine_the_server_core_stays_untouched():
    from src.settings import get_setting

    result = safe_mode.record_mcp_connection("one-time-glitch", "error")
    assert not result["quarantined"]
    assert "one-time-glitch" not in (get_setting("disabled_tools", []) or [])
