"""Lot 40 (QA-36/UX-03), point 3 — `POST /api/chat/regenerate/{sid}`.

Same direct-endpoint-call pattern `tests/test_ask_user_answer_route.py`
already uses for `/api/chat_stream` (extract the real route function off
`chat_routes.setup_chat_routes(...)`'s router and await it with a synthetic
`Request`) rather than a full ASGI `TestClient` — this crosses the real
route code, just without a live server; the underlying model call is faked
the same way every other agent_loop test in this suite fakes it.

Revert proof (COMUN.md rule 5): with the route this lot added removed
(`cp`-backed, never git), `router.routes` has no `/api/chat/regenerate/{sid}`
entry and every test below fails at `_regenerate_endpoint`'s own
`next(...)` lookup — the literal QA-36/UX-03 gap
(`tests/qa/test_qa_36_regenerar_con_efectos.py`'s prior xfail: "no existe un
endpoint/función de 'regenerar' dedicada").
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import routes.chat_routes as chat_routes


class _RouteRequest:
    def __init__(self):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user="alice")


def _session(history):
    added = []
    return SimpleNamespace(
        endpoint_url="https://selected.example/v1",
        model="selected-model",
        headers={},
        name="test",
        history=history,
        add_message=lambda m: added.append(m),
    ), added


def _regenerate_endpoint(monkeypatch, session, *, saved):
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(
        chat_routes, "save_assistant_response",
        lambda *a, **k: saved.append({"args": a, "kwargs": k}) or "msg-db-id",
    )
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(get_session=lambda sid: session, save_sessions=lambda: None),
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )
    return next(r.endpoint for r in router.routes if r.path == "/api/chat/regenerate/{sid}")


async def _drain_json(response):
    events = []
    async for chunk in response.body_iterator:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                events.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _history_with_effect():
    from core.models import ChatMessage
    return [
        ChatMessage("user", "Send Alice a status email and summarize it."),
        ChatMessage("assistant", "Sent the email and here is a summary.", metadata={
            "_db_id": "orig-msg-1",
            "model": "selected-model",
            "tool_events": [{
                "tool": "write_file", "desc": "write_file: notes.txt",
                "command": json.dumps({"path": "notes.txt", "content": "sent"}),
                "output": "wrote notes.txt", "exit_code": 0,
            }],
        }),
    ]


@pytest.mark.asyncio
async def test_effect_tool_is_never_called_again_and_stays_blocked(monkeypatch):
    session, added = _session(_history_with_effect())
    saved = []
    endpoint = _regenerate_endpoint(monkeypatch, session, saved=saved)

    spy_calls = []

    async def fake_agent_stream(endpoint_url, model, messages, **kwargs):
        # What a real agent loop would do: only call the effect tool if the
        # route did NOT block it. This is the "tool de efecto (spy)" the lot
        # asks for — it must never fire.
        if "write_file" not in (kwargs.get("disabled_tools") or set()):
            spy_calls.append(("write_file", "notes.txt"))
        yield 'data: {"delta": "Sent the email (evidence reused) and here is a better summary."}\n\n'
        yield 'data: {"type": "metrics", "data": {"model": "selected-model", "tool_calls": 0}}\n\n'
        yield "data: [DONE]\n\n"

    captured_kwargs = {}

    async def capturing_stream(endpoint_url, model, messages, **kwargs):
        captured_kwargs["messages"] = messages
        captured_kwargs["disabled_tools"] = kwargs.get("disabled_tools")
        async for chunk in fake_agent_stream(endpoint_url, model, messages, **kwargs):
            yield chunk

    monkeypatch.setattr(chat_routes, "stream_agent_loop", capturing_stream)

    response = await endpoint(_RouteRequest(), sid="sess-1")
    await _drain_json(response)

    # The spy never fired: the effectful tool from last time is blocked.
    assert spy_calls == []
    assert "write_file" in captured_kwargs["disabled_tools"]
    # Reads stay available — never blanket-blocked.
    assert "read_file" not in captured_kwargs["disabled_tools"]

    # Evidence reinjected, prior action marked already-done.
    evidence_msg = captured_kwargs["messages"][-1]
    assert evidence_msg["role"] == "system"
    assert "write_file" in evidence_msg["content"]
    assert "already performed" in evidence_msg["content"]
    assert "wrote notes.txt" in evidence_msg["content"]
    # The stale assistant reply itself is NOT replayed as an assistant turn —
    # only the trigger user message plus the evidence note are sent.
    assert [m["role"] for m in captured_kwargs["messages"]] == ["user", "system"]
    assert captured_kwargs["messages"][0]["content"] == "Send Alice a status email and summarize it."

    # regenerated_from links back to the ORIGINAL assistant message.
    assert len(saved) == 1
    saved_metrics = saved[0]["args"][4]
    assert saved_metrics["regenerated_from"] == "orig-msg-1"
    saved_text = saved[0]["args"][3]
    assert saved_text == "Sent the email (evidence reused) and here is a better summary."


@pytest.mark.asyncio
async def test_no_assistant_reply_yet_is_rejected(monkeypatch):
    from core.models import ChatMessage
    session, _ = _session([ChatMessage("user", "hello")])
    saved = []
    endpoint = _regenerate_endpoint(monkeypatch, session, saved=saved)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        await endpoint(_RouteRequest(), sid="sess-2")
    assert excinfo.value.status_code == 400
    assert not saved
