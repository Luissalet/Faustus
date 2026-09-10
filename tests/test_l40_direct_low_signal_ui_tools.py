"""Lot 40 (QA-04/QA-28/QA-36/UX-03/MOD-06), point 1 — EVAL_ESTADO.md's
"Hallazgo NUEVO": the `direct_low_signal` fast path in
`src/agent_loop.py::stream_agent_loop` now offers and parses the two
ALWAYS_AVAILABLE, effect-free UI tools (`ask_user`, `update_plan`) instead
of streaming a valid fence as inert text. Any OTHER fence in that same path
still never executes (CALL-08).

Same direct-generator-call pattern `tests/test_foreground_model_routing.py`
already uses for this path (`agent_loop.stream_agent_loop(...)` collected
with a local `asyncio.run`, `stream_llm_with_fallback` faked) — that file is
not in this lot's PROPIOS, so this is a new file rather than an edit to it.

Revert proof (COMUN.md rule 5): with the parsing block this file's own lot
added to `stream_agent_loop` removed (temporarily, via `cp` — never git),
`test_valid_ask_user_fence_produces_the_card_and_ends_the_turn` fails: no
`ask_user` SSE event is yielded, `tool_calls` stays 0 and `tool_events` is
absent from the metrics — the literal EVAL_ESTADO.md bug.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import src.agent_loop as agent_loop
from src.foreground_model_routing import FOREGROUND_AVAILABILITY_STATUSES


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks):
    out = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                out.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                continue
    return out


@pytest.fixture(autouse=True)
def _isolated_question_store(tmp_path, monkeypatch):
    """Never let this test's `question_store.open_question(...)` call land on
    the process-wide default db (test_ask_user_answer_route.py's own
    `question_db` fixture pattern)."""
    from src import question_store
    path = tmp_path / "l40-questions.sqlite3"
    monkeypatch.setattr(question_store, "default_path", lambda: path)


def _low_signal_kwargs(monkeypatch, text: str):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(
        agent_loop,
        "_classify_agent_request",
        lambda messages, latest: {
            "low_signal": True,
            "continuation": False,
            "domains": [],
            "retrieval_query": latest,
        },
    )
    monkeypatch.setattr(agent_loop, "_is_casual_low_signal", lambda latest: True)
    return {
        "relevant_tools": set(),
        "fallback_statuses": FOREGROUND_AVAILABILITY_STATUSES,
        "fallback_on_empty": False,
    }


def test_valid_ask_user_fence_produces_the_card_and_ends_the_turn(monkeypatch):
    kwargs = _low_signal_kwargs(monkeypatch, "hi")
    fence = (
        '```ask_user\n{"question": "Which greeting?", '
        '"options": [{"label": "Hello"}, {"label": "Hi"}]}\n```'
    )

    async def fake_stream(candidates, messages, **kw):
        yield f'data: {json.dumps({"delta": fence})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    chunks = _collect(agent_loop.stream_agent_loop(
        "https://selected.example/v1", "selected-model",
        [{"role": "user", "content": "hi"}],
        session_id="l40-sess", owner="tester",
        **kwargs,
    ))
    events = _events(chunks)

    ask_user_events = [e for e in events if e.get("type") == "ask_user"]
    assert len(ask_user_events) == 1
    payload = ask_user_events[0]["data"]
    assert payload["question"] == "Which greeting?"
    assert [o["label"] for o in payload["options"]] == ["Hello", "Hi"]
    assert payload["question_id"].startswith("qst_")

    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["direct_low_signal"] is True
    assert metrics["tool_calls"] == 1
    assert metrics["tool_events"][0]["tool"] == "ask_user"
    assert metrics["tool_events"][0]["ask_user"]["question_id"] == payload["question_id"]
    assert "data: [DONE]\n\n" in chunks

    # The registered question is durable, same authority the round-based
    # path uses (CALL-07/TASK-04) — a reload can rebuild the open card.
    from src import question_store
    open_qs = question_store.list_open(owner="tester")
    assert any(q["question_id"] == payload["question_id"] for q in open_qs)


def test_invalid_ask_user_fence_stays_plain_text(monkeypatch):
    """One option only: AskUserTool.execute rejects it — the fence must stay
    inert text exactly like before this lot, not silently drop content."""
    kwargs = _low_signal_kwargs(monkeypatch, "hi")
    fence = '```ask_user\n{"question": "Which?", "options": [{"label": "Only one"}]}\n```'

    async def fake_stream(candidates, messages, **kw):
        yield f'data: {json.dumps({"delta": fence})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    chunks = _collect(agent_loop.stream_agent_loop(
        "https://selected.example/v1", "selected-model",
        [{"role": "user", "content": "hi"}],
        session_id="l40-sess-2", owner="tester",
        **kwargs,
    ))
    events = _events(chunks)

    assert not [e for e in events if e.get("type") == "ask_user"]
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["tool_calls"] == 0
    assert "tool_events" not in metrics


def test_other_fence_in_direct_path_is_never_executed_call08(monkeypatch):
    """CALL-08: a NON-UI tool fence (edit_file) reaching this path is never
    run from text — only ask_user/update_plan are the named exception."""
    kwargs = _low_signal_kwargs(monkeypatch, "hi")
    fence = '```edit_file\n{"path": "a.py", "old_string": "1", "new_string": "2"}\n```'

    async def fake_stream(candidates, messages, **kw):
        yield f'data: {json.dumps({"delta": fence})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    chunks = _collect(agent_loop.stream_agent_loop(
        "https://selected.example/v1", "selected-model",
        [{"role": "user", "content": "hi"}],
        session_id="l40-sess-3", owner="tester",
        **kwargs,
    ))
    events = _events(chunks)

    assert not [e for e in events if e.get("type") in ("ask_user", "tool_start", "tool_output")]
    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["tool_calls"] == 0
    assert "tool_events" not in metrics
    # The raw fence still streams as ordinary text — nothing swallowed.
    streamed = "".join(e["delta"] for e in events if "delta" in e)
    assert streamed == fence


def test_valid_update_plan_fence_emits_plan_update_without_ending_the_turn(monkeypatch):
    kwargs = _low_signal_kwargs(monkeypatch, "hi")
    fence = '```update_plan\n{"plan": "- [x] step one\\n- [ ] step two"}\n```'

    async def fake_stream(candidates, messages, **kw):
        yield f'data: {json.dumps({"delta": fence})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    chunks = _collect(agent_loop.stream_agent_loop(
        "https://selected.example/v1", "selected-model",
        [{"role": "user", "content": "hi"}],
        session_id="l40-sess-4", owner="tester",
        **kwargs,
    ))
    events = _events(chunks)

    plan_events = [e for e in events if e.get("type") == "plan_update"]
    assert len(plan_events) == 1
    assert plan_events[0]["data"]["plan"] == "- [x] step one\n- [ ] step two"
    assert not [e for e in events if e.get("type") == "ask_user"]
    assert "data: [DONE]\n\n" in chunks  # the turn still ends normally
