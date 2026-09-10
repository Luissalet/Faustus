"""L63 · TOOL-03 — a per-server override of the "degraded" thresholds
(docs/spec/v2/backlog.json TOOL-03: "el umbral de degradación configurable
por servidor").

Before this lote `_DEGRADED_ERROR_THRESHOLD`/`_DEGRADED_LATENCY_THRESHOLD_S`/
etc. were class constants — every server shared the exact same bar for
"degraded", with no way to tune one noisy or slow server without lowering
the bar for every other one. `tests/test_l43_tool03_mcp_states.py` keeps
passing unmodified (rule 3): a server nobody ever configured a threshold for
reads exactly the same as before this lote.
"""
from __future__ import annotations

import pytest

from src.mcp_manager import McpManager


@pytest.fixture
def manager():
    return McpManager()


def _connect(manager: McpManager, server_id: str) -> None:
    manager._connections[server_id] = {"status": "connected", "name": server_id}


def test_a_server_with_no_override_reads_the_class_defaults(manager):
    thresholds = manager.degraded_threshold_for("srv1")
    assert thresholds["error_threshold"] == McpManager._DEGRADED_ERROR_THRESHOLD
    assert thresholds["error_window_s"] == McpManager._DEGRADED_ERROR_WINDOW_S
    assert thresholds["latency_threshold_s"] == McpManager._DEGRADED_LATENCY_THRESHOLD_S
    assert thresholds["latency_samples"] == McpManager._DEGRADED_LATENCY_SAMPLES


def test_lowering_one_servers_error_threshold_degrades_it_earlier(manager):
    _connect(manager, "strict-srv")
    manager.set_degraded_threshold("strict-srv", error_threshold=1)
    manager._record_call_outcome("strict-srv", False, 0.1)
    status = manager.get_server_status("strict-srv")
    assert status["status"] == "degraded"
    assert "1 failed tool calls" in status["degraded_reason"]


def test_a_sibling_server_without_an_override_keeps_the_default_threshold(manager):
    """Overriding one server's threshold must never leak onto another one —
    the exact isolation `_call_outcomes` already gives per server_id."""
    _connect(manager, "strict-srv")
    _connect(manager, "normal-srv")
    manager.set_degraded_threshold("strict-srv", error_threshold=1)
    manager._record_call_outcome("normal-srv", False, 0.1)
    manager._record_call_outcome("normal-srv", False, 0.1)
    # 2 errors is under the DEFAULT threshold (3) — normal-srv stays connected.
    assert manager.get_server_status("normal-srv")["status"] == "connected"


def test_raising_the_latency_threshold_stops_slow_calls_from_degrading_it(manager):
    _connect(manager, "tolerant-srv")
    manager.set_degraded_threshold("tolerant-srv", latency_threshold_s=30.0)
    for _ in range(3):
        manager._record_call_outcome("tolerant-srv", True, 9.0)  # would degrade at the default (8.0s)
    assert manager.get_server_status("tolerant-srv")["status"] == "connected"


def test_set_degraded_threshold_only_changes_the_keys_it_was_given(manager):
    manager.set_degraded_threshold("partial-srv", error_threshold=10)
    effective = manager.degraded_threshold_for("partial-srv")
    assert effective["error_threshold"] == 10
    assert effective["latency_threshold_s"] == McpManager._DEGRADED_LATENCY_THRESHOLD_S


def test_a_persisted_setting_also_overrides_the_default(manager, monkeypatch):
    from src import mcp_manager as mm
    monkeypatch.setattr(mm, "_mcp_setting",
                        lambda key, default: {"mcp_degraded_thresholds": {"persisted-srv": {"error_threshold": 1}}}
                        .get(key, default))
    _connect(manager, "persisted-srv")
    manager._record_call_outcome("persisted-srv", False, 0.1)
    status = manager.get_server_status("persisted-srv")
    assert status["status"] == "degraded"


def test_an_in_process_override_wins_over_the_persisted_setting(manager, monkeypatch):
    from src import mcp_manager as mm
    monkeypatch.setattr(mm, "_mcp_setting",
                        lambda key, default: {"mcp_degraded_thresholds": {"both-srv": {"error_threshold": 1}}}
                        .get(key, default))
    manager.set_degraded_threshold("both-srv", error_threshold=99)
    effective = manager.degraded_threshold_for("both-srv")
    assert effective["error_threshold"] == 99
