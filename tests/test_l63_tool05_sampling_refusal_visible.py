"""L63 · TOOL-05 — a refused sampling request becomes a VISIBLE, auditable
`question_store` entry (docs/spec/v2/backlog.json TOOL-05).

Before this lote `make_sampling_callback` refused every request (in-quota or
not) with only a log line — nothing reached `question_store`, so nobody
could see "which MCP server asked to sample the model, and what data it
would have received" without reading the app log. This closes the literal
TOOL-05 acceptance: no indefinite recursion (the callback is synchronous and
never re-enters itself or awaits anything), and no reuse of one server's
authorization by another (every notice is keyed by that server's own
`session_id=f"mcp:{server_id}"`, the same key `_SamplingQuota` already uses
per-server).
"""
from __future__ import annotations

import asyncio

import pytest

from src import mcp_manager, question_store


@pytest.fixture
def questions_db(tmp_path, monkeypatch):
    monkeypatch.setattr(question_store, "default_path", lambda: tmp_path / "questions.db")
    return tmp_path


class _Content:
    def __init__(self, text: str):
        self.text = text


class _Message:
    def __init__(self, text: str):
        self.content = _Content(text)


class _Params:
    def __init__(self, texts):
        self.messages = [_Message(t) for t in texts]


def _open_questions_for_server(server_id: str):
    return [
        q for q in question_store.list_open(limit=50)
        if q["session_id"] == f"mcp:{server_id}"
    ]


# ── the refusal reaches question_store, visibly ────────────────────────────

def test_a_refused_sampling_request_opens_a_visible_question(questions_db, monkeypatch):
    monkeypatch.setattr(mcp_manager, "_sampling_quota", mcp_manager._SamplingQuota())
    callback = mcp_manager.make_sampling_callback("server-visible")

    from mcp import types as mcp_types
    result = asyncio.run(callback(None, _Params(["please summarise this secret memo"])))
    assert isinstance(result, mcp_types.ErrorData)

    open_qs = _open_questions_for_server("server-visible")
    assert len(open_qs) == 1
    q = open_qs[0]
    assert "server-visible" in q["question"]
    assert "summarise this secret memo" in q["question"]
    assert q["status"] == "open"
    assert q["allow_free_text"] is False


def test_the_quota_exhausted_refusal_also_becomes_a_visible_question(questions_db, monkeypatch):
    fresh = mcp_manager._SamplingQuota()
    monkeypatch.setattr(mcp_manager, "_sampling_quota", fresh)
    monkeypatch.setattr(mcp_manager, "_mcp_setting",
                        lambda key, default: {"mcp_sampling_max_calls": 1,
                                              "mcp_sampling_window_s": 60.0}.get(key, default))
    callback = mcp_manager.make_sampling_callback("server-quota")
    asyncio.run(callback(None, _Params(["first request"])))
    asyncio.run(callback(None, _Params(["second request, over quota"])))

    open_qs = _open_questions_for_server("server-quota")
    assert len(open_qs) == 2
    assert any("quota exceeded" in q["question"] for q in open_qs)


def test_a_refusal_with_no_params_still_records_a_notice_without_raising(questions_db, monkeypatch):
    """The existing pre-TOOL-05-lote63 tests call the callback with
    `params=None` — the notice must degrade gracefully, not crash the
    refusal path."""
    monkeypatch.setattr(mcp_manager, "_sampling_quota", mcp_manager._SamplingQuota())
    callback = mcp_manager.make_sampling_callback("server-none")
    from mcp import types as mcp_types
    result = asyncio.run(callback(None, None))
    assert isinstance(result, mcp_types.ErrorData)
    open_qs = _open_questions_for_server("server-none")
    assert len(open_qs) == 1
    assert "preview unavailable" in open_qs[0]["question"] or "no message text" in open_qs[0]["question"]


# ── no reuse of another destination's authorization ────────────────────────

def test_two_servers_refusals_never_share_or_cross_read_each_others_notice(questions_db, monkeypatch):
    monkeypatch.setattr(mcp_manager, "_sampling_quota", mcp_manager._SamplingQuota())
    callback_a = mcp_manager.make_sampling_callback("server-a")
    callback_b = mcp_manager.make_sampling_callback("server-b")

    asyncio.run(callback_a(None, _Params(["server A's private data"])))
    asyncio.run(callback_b(None, _Params(["server B's private data"])))

    a_qs = _open_questions_for_server("server-a")
    b_qs = _open_questions_for_server("server-b")
    assert len(a_qs) == 1 and len(b_qs) == 1
    assert "server A's private data" in a_qs[0]["question"]
    assert "server B's private data" not in a_qs[0]["question"]
    assert "server B's private data" in b_qs[0]["question"]
    assert "server A's private data" not in b_qs[0]["question"]


# ── no indefinite recursion: many refusals in a row still terminate ────────

def test_many_rapid_refusals_from_one_server_terminate_and_each_is_refused(questions_db, monkeypatch):
    """A server that keeps asking gets refused every single time — the
    callback never re-enters itself, never awaits a model, and the quota
    keeps denying deterministically instead of degrading into a hang."""
    fresh = mcp_manager._SamplingQuota()
    monkeypatch.setattr(mcp_manager, "_sampling_quota", fresh)
    monkeypatch.setattr(mcp_manager, "_mcp_setting",
                        lambda key, default: {"mcp_sampling_max_calls": 3,
                                              "mcp_sampling_window_s": 60.0}.get(key, default))
    callback = mcp_manager.make_sampling_callback("server-loop")
    from mcp import types as mcp_types

    results = [asyncio.run(callback(None, _Params([f"request #{i}"]))) for i in range(25)]
    assert len(results) == 25
    assert all(isinstance(r, mcp_types.ErrorData) for r in results)
    # First 3 are the "in-quota, not wired to a model" reason; the rest are
    # the quota-exceeded reason — never anything that looks like success.
    assert sum("quota exceeded" in r.message for r in results) == 22

    # `_SamplingQuota` itself never grows unbounded across a flood of denied
    # calls — only accepted calls are recorded in the rolling window.
    assert len(fresh._calls["server-loop"]) == 3
