"""client_message_id idempotency (UX-02, TASK-03, QA-08): a double click, a
retried fetch after a dropped connection, or a reconnect for the same id must
answer from the outbox instead of starting the turn a second time.

`_chat_stream_endpoint`/`_RouteRequest` are the harness the neighboring
chat_stream tests already use (tests/test_foreground_model_routing.py,
tests/test_chat_stream_user_delegation.py) to drive the real
`@router.post("/api/chat_stream")` function end to end with the model call
itself faked out — reused here rather than reinvented.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import routes.chat_routes as chat_routes
from src import chat_outbox
from src.request_models import ChatRequest
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


@pytest.fixture()
def outbox_dir(tmp_path, monkeypatch):
    """Every test gets its own chat_outbox.sqlite3, never the real one."""
    path = tmp_path / "chat_outbox.sqlite3"
    monkeypatch.setattr(chat_outbox, "default_path", lambda: path)
    return path


# --------------------------------------------------------------------------
# src/chat_outbox.py — the durable store on its own
# --------------------------------------------------------------------------

def test_record_intent_is_idempotent_on_the_unique_index(outbox_dir):
    first = chat_outbox.record_intent(owner="alice", session_id="s1", client_message_id="m1")
    second = chat_outbox.record_intent(owner="alice", session_id="s1", client_message_id="m1")
    assert first["created"] is True and first["status"] == "accepted"
    assert second["created"] is False and second["status"] == "accepted"
    # A different owner or session is a different row entirely.
    assert chat_outbox.get(owner="bob", session_id="s1", client_message_id="m1") is None
    assert chat_outbox.get(owner="alice", session_id="s2", client_message_id="m1") is None


def test_mark_finished_never_resurrects_a_terminal_row(outbox_dir):
    chat_outbox.record_intent(owner="alice", session_id="s1", client_message_id="m1")
    assert chat_outbox.mark_finished(owner="alice", session_id="s1", client_message_id="m1",
                                      status="finished", result={"response": "hi"}) is True
    # A late "failed" write (e.g. a stray cleanup task racing the real
    # completion) must not clobber the terminal state that already landed.
    assert chat_outbox.mark_finished(owner="alice", session_id="s1", client_message_id="m1",
                                      status="failed") is False
    row = chat_outbox.get(owner="alice", session_id="s1", client_message_id="m1")
    assert row["status"] == "finished"
    assert row["result"] == {"response": "hi"}


def test_purge_stale_drops_only_old_terminal_rows(outbox_dir):
    chat_outbox.record_intent(owner="alice", session_id="s1", client_message_id="old")
    chat_outbox.mark_finished(owner="alice", session_id="s1", client_message_id="old", status="finished")
    with sqlite3.connect(str(outbox_dir)) as conn:
        conn.execute("UPDATE outbox SET updated_at='2000-01-01T00:00:00Z' WHERE client_message_id='old'")
        conn.commit()
    chat_outbox.record_intent(owner="alice", session_id="s1", client_message_id="fresh")

    removed = chat_outbox.purge_stale()

    assert removed == 1
    assert chat_outbox.get(owner="alice", session_id="s1", client_message_id="old") is None
    assert chat_outbox.get(owner="alice", session_id="s1", client_message_id="fresh") is not None


# --------------------------------------------------------------------------
# POST /api/chat_stream — the route this lote's test asks for by name
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_chat_stream_post_does_not_start_a_second_turn(monkeypatch, outbox_dir):
    """Two POSTs, same client_message_id: one model call, one outbox row —
    reverting the chat_stream gate (or record_intent/mark_finished) makes
    `calls["model"] == 2` and drops the X-Faustus-Idempotent-Replay header."""
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)

    calls = {"build_context": 0, "model": 0}
    real_build_context = chat_routes.build_chat_context
    real_model_call = chat_routes.stream_llm_with_fallback

    async def counting_build_context(*args, **kwargs):
        calls["build_context"] += 1
        return await real_build_context(*args, **kwargs)

    async def counting_model_call(*args, **kwargs):
        calls["model"] += 1
        async for chunk in real_model_call(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(chat_routes, "build_chat_context", counting_build_context)
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", counting_model_call)

    req1 = _RouteRequest("chat")
    req1._form["client_message_id"] = "cid-double-click"
    response1 = await endpoint(req1)
    assert "X-Faustus-Idempotent-Replay" not in response1.headers

    # The duplicate arrives before response1's body has even been read —
    # the outbox row is still "accepted". It must still be turned away.
    req2 = _RouteRequest("chat")
    req2._form["client_message_id"] = "cid-double-click"
    response2 = await endpoint(req2)
    assert response2.headers.get("X-Faustus-Idempotent-Replay") == "1"

    body1, body2 = b"", b""
    async for chunk in response1.body_iterator:
        body1 += chunk if isinstance(chunk, bytes) else chunk.encode()
    async for chunk in response2.body_iterator:
        body2 += chunk if isinstance(chunk, bytes) else chunk.encode()

    assert calls["build_context"] == 1
    assert calls["model"] == 1
    assert body2 == b"data: [DONE]\n\n"
    assert b"data: [DONE]\n\n" in body1

    row = chat_outbox.get(owner="alice", session_id="session-1", client_message_id="cid-double-click")
    assert row is not None and row["status"] == "finished"

    # A THIRD post, after the first turn has genuinely completed, is answered
    # from the now-finished outbox row rather than opening a new turn.
    req3 = _RouteRequest("chat")
    req3._form["client_message_id"] = "cid-double-click"
    response3 = await endpoint(req3)
    assert response3.headers.get("X-Faustus-Idempotent-Replay") == "1"
    async for _ in response3.body_iterator:
        pass
    assert calls["build_context"] == 1
    assert calls["model"] == 1


@pytest.mark.asyncio
async def test_different_client_message_id_is_its_own_turn(monkeypatch, outbox_dir):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    calls = {"model": 0}
    real_model_call = chat_routes.stream_llm_with_fallback

    async def counting_model_call(*args, **kwargs):
        calls["model"] += 1
        async for chunk in real_model_call(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", counting_model_call)

    for cid in ("cid-a", "cid-b"):
        req = _RouteRequest("chat")
        req._form["client_message_id"] = cid
        response = await endpoint(req)
        assert "X-Faustus-Idempotent-Replay" not in response.headers
        async for _ in response.body_iterator:
            pass

    assert calls["model"] == 2


@pytest.mark.asyncio
async def test_client_message_id_is_optional(monkeypatch, outbox_dir):
    """Absent id: every send behaves exactly as before — no outbox row, no
    header, no throttling of repeated sends for the same session."""
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    calls = {"model": 0}
    real_model_call = chat_routes.stream_llm_with_fallback

    async def counting_model_call(*args, **kwargs):
        calls["model"] += 1
        async for chunk in real_model_call(*args, **kwargs):
            yield chunk

    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", counting_model_call)

    for _ in range(2):
        response = await endpoint(_RouteRequest("chat"))
        assert "X-Faustus-Idempotent-Replay" not in response.headers
        async for _ in response.body_iterator:
            pass

    assert calls["model"] == 2
    assert not outbox_dir.exists()


# --------------------------------------------------------------------------
# POST /api/chat (non-streaming) — the same key, optional here too
# --------------------------------------------------------------------------

class _ChatHandler:
    async def handle_memory_command(self, _session, _message):
        return None


class _JsonRequest:
    """Just enough of Starlette's Request for chat_endpoint: it only reads
    `.json()` directly (everything else goes through mocked helpers)."""

    def __init__(self, payload):
        self._payload = payload
        self.headers: dict = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user="alice")

    async def json(self):
        return self._payload


def _chat_endpoint_harness(monkeypatch):
    added = []
    session = SimpleNamespace(
        endpoint_url="https://selected.example/v1",
        model="selected-model",
        headers={"Authorization": "Bearer selected"},
        name="test",
        history=[],
        add_message=added.append,
    )
    session_manager = SimpleNamespace(
        get_session=lambda session_id: session,
        save_sessions=lambda: None,
    )
    context = SimpleNamespace(
        user="alice",
        messages=[{"role": "user", "content": "hello"}],
        route_messages=[{"role": "user", "content": "hello"}],
        preface=[],
        preset=SimpleNamespace(temperature=0.2, max_tokens=128, character_name=None),
        context_length=4096,
        uprefs={},
    )
    calls = {"build_context": 0, "model": 0}

    async def fake_build_context(*args, **kwargs):
        calls["build_context"] += 1
        return context

    async def fake_llm_call(*args, **kwargs):
        calls["model"] += 1
        return "the reply", "candidate-0", "selected-model"

    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(chat_routes, "_clear_orphaned_session_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_recover_empty_session_model", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "build_chat_context", fake_build_context)
    monkeypatch.setattr(chat_routes, "llm_call_async_with_route_fallback", fake_llm_call)
    monkeypatch.setattr(chat_routes, "_candidate_index", lambda candidates, actual: 0)
    monkeypatch.setattr(chat_routes, "apply_compaction_state", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "clean_thinking_for_save", lambda reply, meta: (reply, meta))
    monkeypatch.setattr(chat_routes, "run_post_response_tasks", lambda *a, **k: None)
    monkeypatch.setattr(
        chat_routes, "resolve_foreground_model_policy",
        lambda **k: SimpleNamespace(enabled=False, eligible_statuses=(), fallback_on_empty=False),
    )
    monkeypatch.setattr(chat_routes, "build_foreground_model_candidates", lambda *a, **k: ["candidate-0"])
    monkeypatch.setattr(
        chat_routes, "build_foreground_route_descriptors",
        lambda *a, **k: [{"endpoint_id": "selected", "endpoint_label": "Selected"}],
    )
    monkeypatch.setattr("core.database.update_session_last_accessed", lambda *a, **k: None)

    router = chat_routes.setup_chat_routes(
        session_manager, _ChatHandler(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/chat")
    return endpoint, calls, added


@pytest.mark.asyncio
async def test_duplicate_chat_post_replays_the_saved_result(monkeypatch, outbox_dir):
    endpoint, calls, added = _chat_endpoint_harness(monkeypatch)
    chat_request = ChatRequest(message="hello", session="session-1")

    result1 = await endpoint(_JsonRequest({"client_message_id": "cid-1"}), chat_request)
    assert result1["response"] == "the reply"
    assert "idempotent_replay" not in result1

    result2 = await endpoint(_JsonRequest({"client_message_id": "cid-1"}), chat_request)
    assert result2["idempotent_replay"] is True
    assert result2["response"] == "the reply"

    assert calls["model"] == 1
    assert len(added) == 1  # exactly one assistant message persisted


@pytest.mark.asyncio
async def test_chat_post_without_client_message_id_is_unaffected(monkeypatch, outbox_dir):
    endpoint, calls, added = _chat_endpoint_harness(monkeypatch)
    chat_request = ChatRequest(message="hello", session="session-1")

    for _ in range(2):
        result = await endpoint(_JsonRequest({}), chat_request)
        assert "idempotent_replay" not in result

    assert calls["model"] == 2
    assert len(added) == 2
    assert not outbox_dir.exists()
