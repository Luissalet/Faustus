"""Lote 70a, punto A.1 — the live SSE `ask_user` card (both the round-based
tool-call path and the `direct_low_signal` fast path in
`src/agent_loop.py::stream_agent_loop`) must carry the same `revision` field
`GET /api/questions`/a history reload would give the card, exactly as
`question_store.open_question(...)` returns it — otherwise an answer sent
against the live card can never be checked for staleness
(`routes/chat_routes.py::_parse_question_revision`,
`tests/test_l61_ux_ask_user_revision.py`) because the field was never on the
wire in the first place.

Before this lot: `_auq`/`_direct_ask_user_payload` were built straight from
`AskUserTool`'s own return value, which has no `revision` key —
`question_store.open_question(...)` was called only for its side effect
(durable registration), its return value discarded. After: the returned
row's `revision` is copied back onto the payload that is actually streamed.
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
    """Same isolation `tests/test_l40_direct_low_signal_ui_tools.py` uses —
    never let `question_store.open_question(...)` land on the process-wide
    default db shared with other test files."""
    from src import question_store
    path = tmp_path / "l70-a01-questions.sqlite3"
    monkeypatch.setattr(question_store, "default_path", lambda: path)


def test_round_based_ask_user_event_carries_revision(monkeypatch):
    """`tests/test_ask_user_persistence.py`'s own fixture, with a real
    `session_id`/`owner` so `question_store.open_question` actually runs
    (it no-ops silently without a `session_id`) and the returned `revision`
    is asserted on both the live SSE event and the persisted tool_event."""
    payload = {
        "question": "Which project?",
        "options": [{"label": "Reviews"}, {"label": "Topics"}],
        "multi": False,
    }

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def fake_stream(_candidates, messages, **kwargs):
        call = {"name": "ask_user", "arguments": json.dumps(payload, ensure_ascii=False)}
        yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        parsed = json.loads(block.content)
        return (
            "ask_user",
            {"ask_user": parsed, "output": "Awaiting their selection.", "exit_code": 0},
        )

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    monkeypatch.setattr(agent_loop, "execute_tool_block", fake_execute, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1", "gpt-4o",
            [{"role": "user", "content": "Help me pick a project."}],
            session_id="l70-a01-round", owner="tester",
            relevant_tools={"ask_user"}, _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    ask_user_event = next(e for e in events if e.get("type") == "ask_user")
    assert ask_user_event["data"]["revision"] == 1

    tool_output = next(e for e in events if e.get("type") == "tool_output")
    assert tool_output["ask_user"]["revision"] == 1

    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["tool_events"][0]["ask_user"]["revision"] == 1

    from src import question_store
    stored = question_store.list_open(owner="tester")
    assert stored and stored[0]["revision"] == 1


def test_direct_low_signal_ask_user_event_carries_revision(monkeypatch):
    """Same field, the OTHER path: `direct_low_signal`'s own inline fence
    parser (~L5111 in `src/agent_loop.py`)."""
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(
        agent_loop, "_classify_agent_request",
        lambda messages, latest: {
            "low_signal": True, "continuation": False, "domains": [], "retrieval_query": latest,
        },
    )
    monkeypatch.setattr(agent_loop, "_is_casual_low_signal", lambda latest: True)

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
        session_id="l70-a01-direct", owner="tester",
        relevant_tools=set(),
        fallback_statuses=FOREGROUND_AVAILABILITY_STATUSES,
        fallback_on_empty=False,
    ))
    events = _events(chunks)

    ask_user_event = next(e for e in events if e.get("type") == "ask_user")
    assert ask_user_event["data"]["revision"] == 1

    metrics = next(e["data"] for e in events if e.get("type") == "metrics")
    assert metrics["tool_events"][0]["ask_user"]["revision"] == 1
