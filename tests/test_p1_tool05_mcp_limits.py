"""TOOL-05 — elicitation, sampling and resources under limits.

Acceptance: a server cannot recurse indefinitely asking for models, nor
reuse another destination's authorization.
"""
from __future__ import annotations

import asyncio

import pytest

from src import mcp_manager


# ── sampling: per-server quota, never shared across servers ────────────────

def test_sampling_quota_refuses_once_a_server_exceeds_its_window():
    quota = mcp_manager._SamplingQuota()
    for _ in range(3):
        assert quota.consume("server-a", max_calls=3, window_s=60.0) is None
    denial = quota.consume("server-a", max_calls=3, window_s=60.0)
    assert denial is not None
    assert "quota exceeded" in denial


def test_sampling_quota_is_never_shared_across_servers():
    """One server's exhausted quota must not borrow from — or spend —
    another server's own allowance (the literal "reuse another
    destination's authorization" acceptance case)."""
    quota = mcp_manager._SamplingQuota()
    for _ in range(3):
        assert quota.consume("server-a", max_calls=3, window_s=60.0) is None
    assert quota.consume("server-a", max_calls=3, window_s=60.0) is not None
    # server-b has spent nothing and is refused nothing.
    assert quota.consume("server-b", max_calls=3, window_s=60.0) is None


def test_sampling_callback_denies_in_quota_with_a_distinct_reason(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_sampling_quota", mcp_manager._SamplingQuota())
    monkeypatch.setattr(mcp_manager, "_mcp_setting",
                        lambda key, default: {"mcp_sampling_max_calls": 5,
                                              "mcp_sampling_window_s": 60.0}.get(key, default))
    callback = mcp_manager.make_sampling_callback("server-x")
    result = asyncio.run(callback(None, None))
    from mcp import types as mcp_types
    assert isinstance(result, mcp_types.ErrorData)
    assert "not yet connected to a model" in result.message


def test_sampling_callback_reports_quota_exhaustion_distinctly(monkeypatch):
    fresh = mcp_manager._SamplingQuota()
    monkeypatch.setattr(mcp_manager, "_sampling_quota", fresh)
    monkeypatch.setattr(mcp_manager, "_mcp_setting",
                        lambda key, default: {"mcp_sampling_max_calls": 1,
                                              "mcp_sampling_window_s": 60.0}.get(key, default))
    callback = mcp_manager.make_sampling_callback("server-y")
    from mcp import types as mcp_types
    first = asyncio.run(callback(None, None))
    second = asyncio.run(callback(None, None))
    assert isinstance(first, mcp_types.ErrorData) and "not yet connected" in first.message
    assert isinstance(second, mcp_types.ErrorData) and "quota exceeded" in second.message


# ── elicitation: outstanding cap, size cap, and a real timeout path ────────

def test_elicitation_refuses_over_the_outstanding_cap(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_mcp_setting",
                        lambda key, default: {"mcp_elicitation_max_outstanding": 2}.get(key, default))
    mcp_manager._elicit_outstanding["server-z"] = 2
    try:
        callback = mcp_manager.make_elicitation_callback("server-z")

        class _Params:
            message = "may I have your email?"

        from mcp import types as mcp_types
        result = asyncio.run(callback(None, _Params()))
        assert isinstance(result, mcp_types.ErrorData)
        assert "too many outstanding" in result.message
    finally:
        mcp_manager._elicit_outstanding.pop("server-z", None)


def test_elicitation_rejects_an_empty_message(monkeypatch):
    monkeypatch.setattr(mcp_manager, "_mcp_setting", lambda key, default: default)
    callback = mcp_manager.make_elicitation_callback("server-empty")

    class _Params:
        message = "   "

    from mcp import types as mcp_types
    result = asyncio.run(callback(None, _Params()))
    assert isinstance(result, mcp_types.ErrorData)


def test_elicitation_times_out_and_leaves_no_outstanding_count(monkeypatch, tmp_path):
    """An unanswered question is cancelled, not awaited forever, and the
    per-server outstanding counter is released either way."""
    import src.question_store as question_store
    monkeypatch.setattr(question_store, "default_path", lambda: tmp_path / "questions.db")
    monkeypatch.setattr(mcp_manager, "_mcp_setting",
                        lambda key, default: {
                            "mcp_elicitation_timeout_s": 0.2,
                        }.get(key, default))
    monkeypatch.setattr(mcp_manager, "_ELICIT_POLL_INTERVAL_S", 0.05)
    callback = mcp_manager.make_elicitation_callback("server-timeout")

    class _Params:
        message = "confirm this action?"

    from mcp import types as mcp_types
    result = asyncio.run(callback(None, _Params()))
    assert isinstance(result, mcp_types.ElicitResult)
    assert result.action == "cancel"
    assert mcp_manager._elicit_outstanding.get("server-timeout", 0) == 0


# ── roots: advisory only ────────────────────────────────────────────────────

def test_list_roots_returns_no_roots_without_an_active_workspace(monkeypatch):
    import src.tool_execution as tool_execution
    monkeypatch.setattr(tool_execution, "get_active_workspace", lambda: None)
    callback = mcp_manager.make_list_roots_callback("server-r")
    result = asyncio.run(callback(None))
    assert result.roots == []
