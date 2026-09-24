"""Tests for `src.drift_check.DriftCheckState` — the turn-scoped hook
between the agent harness and `src.code_graph.drift`, exercised directly
(no full agent_loop turn) against fakes for the code_graph calls, so this
never depends on Louvain actually clustering anything."""
from __future__ import annotations

import pytest

from src import drift_check


class _FakeStatus:
    def __init__(self, files: int):
        self._files = files

    def get(self, key, default=None):
        return self._files if key == "files" else default


def _patch_setting(monkeypatch, values: dict):
    def fake_get_setting(key, default=None):
        return values.get(key, default)
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", fake_get_setting)


def test_no_workspace_is_a_permanent_no_op():
    state = drift_check.DriftCheckState(None)
    assert state.enabled is False
    state.snapshot_before_first_edit()
    assert state.baseline_id is None
    assert state.run_after_turn() is None


def test_setting_off_disables_the_check(monkeypatch):
    _patch_setting(monkeypatch, {"code_graph_drift_check": False})
    state = drift_check.DriftCheckState("/some/workspace")
    assert state.enabled is False


def test_tiny_workspace_disables_the_check(monkeypatch):
    _patch_setting(monkeypatch, {"code_graph_drift_check": True, "code_graph_drift_min_files": 8})
    state = drift_check.DriftCheckState("/some/workspace")

    from src.context_engine import code_index
    monkeypatch.setattr(code_index, "status", lambda root, project_id="": {"files": 3})

    state.snapshot_before_first_edit()
    assert state.enabled is False
    assert state.snapshot_taken is True
    assert state.baseline_id is None


def test_snapshot_success_records_a_baseline_id(monkeypatch):
    _patch_setting(monkeypatch, {"code_graph_drift_check": True, "code_graph_drift_min_files": 1})

    from src.context_engine import code_index
    monkeypatch.setattr(code_index, "status", lambda root, project_id="": {"files": 100})

    import src.code_graph as code_graph
    monkeypatch.setattr(code_graph, "snapshot",
                        lambda root, project_id="", label="": {"exit_code": 0, "baseline_id": "bl_test123"})

    state = drift_check.DriftCheckState("/ws")
    state.snapshot_before_first_edit()
    assert state.enabled is True
    assert state.baseline_id == "bl_test123"
    assert state.snapshot_taken is True

    # A second call is a strict no-op (idempotent).
    state.snapshot_before_first_edit()
    assert state.baseline_id == "bl_test123"


def test_snapshot_failure_disables_the_rest_of_the_turn(monkeypatch):
    _patch_setting(monkeypatch, {"code_graph_drift_check": True, "code_graph_drift_min_files": 1})

    from src.context_engine import code_index
    monkeypatch.setattr(code_index, "status", lambda root, project_id="": {"files": 100})

    import src.code_graph as code_graph
    monkeypatch.setattr(code_graph, "snapshot",
                        lambda root, project_id="", label="": {"exit_code": 1, "error": "boom"})

    state = drift_check.DriftCheckState("/ws")
    state.snapshot_before_first_edit()
    assert state.enabled is False
    assert state.baseline_id is None


def test_run_after_turn_without_a_baseline_is_none():
    state = drift_check.DriftCheckState("/ws")
    state.enabled = True
    assert state.run_after_turn() is None


def test_run_after_turn_below_threshold_returns_none(monkeypatch):
    _patch_setting(monkeypatch, {
        "code_graph_drift_check": True, "code_graph_drift_note_threshold": 25,
        "code_graph_drift_time_budget_s": 5.0,
    })
    import src.code_graph as code_graph
    monkeypatch.setattr(code_graph, "drift", lambda root, project_id="", baseline_id="", time_budget_s=8.0: {
        "exit_code": 0, "score": 10, "top_findings": [],
    })

    state = drift_check.DriftCheckState("/ws")
    state.enabled = True
    state.baseline_id = "bl_x"
    assert state.run_after_turn() is None


def test_run_after_turn_above_threshold_returns_a_note(monkeypatch):
    _patch_setting(monkeypatch, {
        "code_graph_drift_check": True, "code_graph_drift_note_threshold": 25,
        "code_graph_drift_time_budget_s": 5.0,
    })
    import src.code_graph as code_graph
    monkeypatch.setattr(code_graph, "drift", lambda root, project_id="", baseline_id="", time_budget_s=8.0: {
        "exit_code": 0, "score": 40,
        "top_findings": [{"explanation": "a new cross-community edge appeared"}],
    })

    state = drift_check.DriftCheckState("/ws")
    state.enabled = True
    state.baseline_id = "bl_x"
    note = state.run_after_turn()
    assert note is not None
    assert "40/100" in note
    assert "cross-community edge" in note
    assert state.note == note


def test_run_after_turn_swallows_exceptions(monkeypatch):
    _patch_setting(monkeypatch, {"code_graph_drift_check": True})

    def _boom(*a, **k):
        raise RuntimeError("boom")

    import src.code_graph as code_graph
    monkeypatch.setattr(code_graph, "drift", _boom)

    state = drift_check.DriftCheckState("/ws")
    state.enabled = True
    state.baseline_id = "bl_x"
    assert state.run_after_turn() is None
