"""CALL-07 / TASK-04: /api/chat_stream must resolve a question_id answer
BEFORE starting a turn, and a rejected answer must persist nothing and
start no turn at all.

Uses the real `/api/chat_stream` route function (see
tests/test_foreground_model_routing.py's `_chat_stream_endpoint` pattern,
which this mirrors) rather than mocking chat_routes.chat_stream itself, so
the HTTP-crossing status code and JSON body are the ones a real client
would see. Reverting routes/chat_routes.py's question_id gate (removing the
early `question_store.resolve_question(...)` check) makes every "_rejected"
test below fail: the model would get called and a 200 stream returned
instead of a 409.
"""
import json
from types import SimpleNamespace

import pytest

import src.agent_tools  # noqa: F401 - resolves circular schema imports first
import routes.chat_routes as chat_routes
import src.agent_runs as agent_runs
from src import question_store

_TEST_SESSION_ID = "askuser-route-test-session"


@pytest.fixture(autouse=True)
def _clean_agent_runs():
    """`agent_runs._RUNS` is a real module-global dict, not routed through
    any per-test tmp path — a fake `stream_agent_loop`/`stream_llm_with_fallback`
    still goes through the endpoint's real `agent_runs.start(session, ...)`
    call, so a run left registered here for `_TEST_SESSION_ID` would leak
    into (and change the observed behavior of) an unrelated test file that
    happens to reuse the same session id. Clean up on both sides."""
    agent_runs.stop_for_session(_TEST_SESSION_ID)
    yield
    agent_runs.stop_for_session(_TEST_SESSION_ID)


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
    def __init__(self, extra_form=None):
        self.headers = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user="alice")
        self._form = {
            "message": "my answer",
            "session": "askuser-route-test-session",
            "mode": "chat",
        }
        if extra_form:
            self._form.update(extra_form)

    async def form(self):
        return self._form


def _chat_stream_endpoint(monkeypatch, captured):
    def add_message(message):
        captured.setdefault("added_messages", []).append(message)

    session = SimpleNamespace(
        endpoint_url="https://selected.example/v1",
        model="selected-model",
        headers={},
        name="test",
        history=[],
        add_message=add_message,
    )
    session_manager = SimpleNamespace(
        get_session=lambda session_id: session,
        save_sessions=lambda: None,
    )
    context = SimpleNamespace(
        user="alice",
        messages=[{"role": "user", "content": "my answer"}],
        route_messages=[{"role": "user", "content": "my answer"}],
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
        captured["build_context_called"] = True
        return context

    async def fake_chat_stream(candidates, messages, **kwargs):
        captured["chat_called"] = True
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_agent_stream(endpoint_url, model, messages, **kwargs):
        captured["agent_called"] = True
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "coerce_message_and_session", lambda *a, **k: ("my answer", "askuser-route-test-session"))
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    monkeypatch.setattr(chat_routes, "_clear_orphaned_session_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_recover_empty_session_model", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "resolve_session_auth", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "get_session_mode", lambda session_id: "chat")
    monkeypatch.setattr(chat_routes, "set_session_mode", lambda *a, **k: None)
    monkeypatch.setattr(chat_routes, "build_chat_context", fake_build_context)
    monkeypatch.setattr(chat_routes, "SessionLocal", _EmptyDb)
    monkeypatch.setattr(chat_routes, "_is_image_generation_session", lambda *a, **k: False)
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", fake_chat_stream)
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

    monkeypatch.setattr(
        chat_routes.chat_outbox,
        "record_intent",
        lambda **k: captured.setdefault("outbox_intent", []).append(k),
    )
    monkeypatch.setattr(chat_routes.chat_outbox, "get", lambda **k: None)

    router = chat_routes.setup_chat_routes(
        session_manager,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    return next(route.endpoint for route in router.routes if route.path == "/api/chat_stream")


@pytest.fixture
def question_db(tmp_path, monkeypatch):
    """An isolated question_store db, like test_chat_idempotency.py's
    outbox_dir fixture pattern for chat_outbox: every `Store()` instance the
    route creates (module-level `resolve_question` mints a fresh one per
    call) must land on the same tmp file, not the process-wide default."""
    path = tmp_path / "questions.sqlite3"
    monkeypatch.setattr(question_store, "default_path", lambda: path)
    return question_store.Store(path)


async def _drain(response):
    if hasattr(response, "body_iterator"):
        async for _ in response.body_iterator:
            pass
    return response


@pytest.mark.asyncio
async def test_open_question_id_proceeds_normally(monkeypatch, question_db):
    q = question_db.open("Which library?", session_id="askuser-route-test-session", options=[{"label": "SQLite", "id": "opt_a"}])
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({"question_id": q["question_id"], "option_ids": json.dumps(["opt_a"])}))
    await _drain(response)

    assert captured.get("chat_called") or captured.get("agent_called")
    resolved = question_db.get(q["question_id"])
    assert resolved["status"] == "answered"
    assert resolved["answer"]["option_ids"] == ["opt_a"]


@pytest.mark.asyncio
async def test_cancelled_question_id_is_rejected_with_409(monkeypatch, question_db):
    q = question_db.open("Which library?", session_id="askuser-route-test-session")
    question_db.cancel(q["question_id"], reason="superseded")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({"question_id": q["question_id"]}))

    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    assert body == {
        "error": "question_not_resolved",
        "reason": "cancelled",
        "question_id": q["question_id"],
        "detail": "superseded",
    }
    assert "chat_called" not in captured
    assert "agent_called" not in captured
    assert "build_context_called" not in captured
    assert "outbox_intent" not in captured


@pytest.mark.asyncio
async def test_already_answered_question_id_is_rejected_with_409(monkeypatch, question_db):
    q = question_db.open("Which library?", session_id="askuser-route-test-session")
    question_db.resolve(q["question_id"], {"text": "first answer"})
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({"question_id": q["question_id"]}))

    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    assert body["reason"] == "already_answered"
    assert body["question_id"] == q["question_id"]
    assert "chat_called" not in captured
    assert "agent_called" not in captured


@pytest.mark.asyncio
async def test_stale_revision_question_id_is_rejected_with_409(monkeypatch, question_db):
    q = question_db.open("Which library?", session_id="askuser-route-test-session", revision=2)
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    # A route-level revision isn't sent by today's Studio; this exercises
    # question_store's own stale-revision path directly to prove the 409
    # branch above surfaces whatever reason resolve_question() returns.
    def _stale_resolve(question_id, answer, *, revision=None):
        return question_store.Store().resolve(question_id, answer, revision=999)

    monkeypatch.setattr(question_store, "resolve_question", _stale_resolve)

    response = await endpoint(_RouteRequest({"question_id": q["question_id"]}))

    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    assert body["reason"] == "stale_revision"
    assert "chat_called" not in captured
    assert "agent_called" not in captured


@pytest.mark.asyncio
async def test_expired_question_id_is_rejected_with_409(monkeypatch, question_db):
    # ttl_seconds is clamped to at least 1 second in the future by
    # question_store._expires, so force expiry directly instead of racing
    # the clock: expire_stale(now=<far future>) is the store's own sweep,
    # just run with a `now` that's already past this question's deadline.
    q = question_db.open("Which library?", session_id="askuser-route-test-session", ttl_seconds=1)
    question_db.expire_stale(now="2999-01-01T00:00:00Z")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({"question_id": q["question_id"]}))

    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    assert body["reason"] == "expired"
    assert "chat_called" not in captured
    assert "agent_called" not in captured


@pytest.mark.asyncio
async def test_unknown_question_id_is_rejected_with_409(monkeypatch, question_db):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest({"question_id": "qst_does_not_exist"}))

    assert response.status_code == 409
    body = json.loads(bytes(response.body))
    assert body["reason"] == "not_found"
    assert body["question_id"] == "qst_does_not_exist"
    assert "chat_called" not in captured
    assert "agent_called" not in captured


@pytest.mark.asyncio
async def test_absent_question_id_behaves_as_before(monkeypatch, question_db):
    """No question_id at all: today's behavior, completely unaffected."""
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    response = await endpoint(_RouteRequest())
    await _drain(response)

    assert captured.get("chat_called") or captured.get("agent_called")


@pytest.mark.asyncio
async def test_tool_approval_continuation_skips_the_question_gate(monkeypatch, question_db):
    """A tool-approval continuation is a different single-use gate entirely
    (tool_approval_store) — a stray question_id alongside it must not be
    treated as an ask_user answer."""
    q = question_db.open("Which library?", session_id="askuser-route-test-session")
    question_db.cancel(q["question_id"])
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, captured)

    # tool_approval_store.peek('approval-1') is not mocked here, so an
    # unknown approval id raises the tool-approval route's OWN 409
    # (tool_approval_invalid) well before our gate would ever run again —
    # proof enough that the question_id gate was skipped: our 409 carries
    # `{"error": "question_not_resolved", ...}`, a completely different
    # shape, and never as a raised HTTPException.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(_RouteRequest({"question_id": q["question_id"], "tool_approval_id": "approval-1"}))
    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert not (isinstance(detail, dict) and detail.get("error") == "question_not_resolved")
