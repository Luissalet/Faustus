"""Lote 44 (closes a Lote 40 gap): `routes/chat_routes.py`'s `/api/chat_stream`
SSE forwarding switch is a strict allowlist of `data["type"]` values — a type
`stream_agent_loop` yields but this switch does not name is computed by the
agent loop and then silently dropped right here, the same shape of gap
`tests/test_lote20_chat_routes_budget_exhausted.py` already proved for
`budget_exhausted`. `"capabilities_changed"` (`src/agent_loop.py`'s
`_cap_switch` block, MOD-06/QA-28, already covered end-to-end at the
`stream_agent_loop` generator level by
`tests/test_l40_capabilities_on_model_switch.py`) was one such type: never
reached the HTTP client.

Mirrors `tests/test_lote20_chat_routes_budget_exhausted.py`'s own pattern —
the real `/api/chat_stream` route function pulled off `setup_chat_routes(...)`
and awaited with a synthetic `Request`, `stream_agent_loop` faked to yield a
`capabilities_changed` chunk — per COMUN.md's "crosses an HTTP boundary, use
the real route" rule applied without a live TestClient server.

Revert proof (COMUN.md rule 5): with `"capabilities_changed"` removed from
the allowlist tuple this lote added it to (`cp`-backed, never git), this
test's `assert any(...)` fails — the event never reaches `emitted`.
"""
import json
from types import SimpleNamespace

import pytest

import routes.chat_routes as chat_routes


class _EmptyQuery:
    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return None


class _EmptyDb:
    def query(self, *a, **k):
        return _EmptyQuery()

    def close(self):
        return None


class _RouteRequest:
    def __init__(self):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user="alice")
        self._form = {"message": "hello", "session": "session-cap-changed", "mode": "agent"}

    async def form(self):
        return self._form


def _chat_stream_endpoint(monkeypatch, agent_chunks):
    session = SimpleNamespace(
        endpoint_url="https://selected.example/v1",
        model="selected-model",
        headers={},
        name="test",
        history=[],
        add_message=lambda message: None,
    )
    session_manager = SimpleNamespace(
        get_session=lambda session_id: session,
        save_sessions=lambda: None,
    )
    context = SimpleNamespace(
        user="alice",
        messages=[{"role": "user", "content": "hello"}],
        route_messages=[{"role": "user", "content": "hello"}],
        preprocessed=SimpleNamespace(attachment_meta=[]),
        auto_opened_docs=[],
        rag_sources=[],
        web_sources=[],
        used_memories=[],
        uploaded_files=[],
        uprefs={},
        was_compacted=False,
        context_trimmed=False,
        context_length=4096,
        context_messages_before_trim=1,
        context_messages_after_trim=1,
        context_tokens_before_trim=10,
        context_tokens_after_trim=10,
        preset=SimpleNamespace(temperature=0.2, max_tokens=128, character_name=None),
    )

    async def fake_build_context(*a, **k):
        return context

    async def fake_agent_stream(endpoint_url, model, messages, **kwargs):
        for chunk in agent_chunks:
            yield chunk

    monkeypatch.setattr(chat_routes, "coerce_message_and_session", lambda *a, **k: ("hello", "session-cap-changed"))
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(chat_routes, "_clear_orphaned_session_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_recover_empty_session_model", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "resolve_session_auth", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "get_session_mode", lambda session_id: "agent")
    monkeypatch.setattr(chat_routes, "set_session_mode", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "build_chat_context", fake_build_context)
    monkeypatch.setattr(chat_routes, "SessionLocal", _EmptyDb)
    monkeypatch.setattr(chat_routes, "_is_image_generation_session", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "stream_agent_loop", fake_agent_stream)
    monkeypatch.setattr(chat_routes, "save_assistant_response", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "run_post_response_tasks", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "estimate_tokens", lambda messages: 10)
    monkeypatch.setattr(chat_routes, "accumulate_token_usage", lambda *a, **k: None)

    import src.foreground_model_routing as foreground_model_routing

    monkeypatch.setattr(foreground_model_routing, "_load_policy_preferences", lambda owner=None: {})
    import src.settings as settings

    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(settings, "get_user_setting", lambda key, owner="", default=None: default)

    router = chat_routes.setup_chat_routes(
        session_manager,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    return next(route.endpoint for route in router.routes if route.path == "/api/chat_stream")


@pytest.mark.asyncio
async def test_capabilities_changed_event_reaches_the_client():
    monkeypatch = pytest.MonkeyPatch()
    try:
        cap_changed_chunk = (
            'data: ' + json.dumps({
                "type": "capabilities_changed",
                "data": {"from_model": "selected-model", "to_model": "backup-model",
                          "lost": ["vision", "native tool calling"]},
                "round": 2,
            }) + '\n\n'
        )
        agent_chunks = [
            'data: ' + json.dumps({"delta": "partial"}) + '\n\n',
            cap_changed_chunk,
            'data: ' + json.dumps({"delta": " answer"}) + '\n\n',
            "data: [DONE]\n\n",
        ]
        endpoint = _chat_stream_endpoint(monkeypatch, agent_chunks)
        response = await endpoint(_RouteRequest())
        emitted = [chunk async for chunk in response.body_iterator]
    finally:
        monkeypatch.undo()

    # Compare decoded payloads, not raw bytes: every event on this stream now
    # additionally carries the OBS-01/QA-09 observability fields (trace_id,
    # step_id, sequence, stream_id, schema_version), which is additive and
    # must not defeat this assertion (COMUN.md back-compat rule).
    decoded = [json.loads(chunk[len("data: "):]) for chunk in emitted
               if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]")]
    expected = json.loads(cap_changed_chunk[len("data: "):])
    assert any(expected.items() <= d.items() for d in decoded), emitted
