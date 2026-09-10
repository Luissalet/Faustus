"""Lote 61 — UX-02/TASK-03: when a duplicate `client_message_id` arrives and
the FIRST attempt's outcome is not actually known yet (still `accepted`/
`running` in `src/chat_outbox.py`, most often because the process restarted
mid-turn and never reached `mark_finished`), the replay must say so honestly
-- "uncertain, comprobando" -- instead of a bare `[DONE]` a caller could read
as "it finished, and finished fine."

Before this lote: `routes/chat_routes.py::_idempotent_replay_stream` fell
straight to a silent `[DONE]` whenever `agent_runs.subscribe` found no live
run to reattach to, with no way to tell "the run just finished a moment ago
(the common case, and genuinely fine)" apart from "the run never finished at
all (a crash) and nobody knows what happened." The non-streaming `/api/chat`
JSON reply had the same gap: an `accepted`/`running` row and a `finished`
row with a dropped body both looked identical to the caller.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import routes.chat_routes as chat_routes
import src.agent_runs as agent_runs
from src import chat_outbox
from src.request_models import ChatRequest
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


# ---------------------------------------------------------------------------
# _idempotent_replay_stream — the streaming primitive on its own
# ---------------------------------------------------------------------------

@pytest.fixture()
def outbox_dir(tmp_path, monkeypatch):
    path = tmp_path / "chat_outbox.sqlite3"
    monkeypatch.setattr(chat_outbox, "default_path", lambda: path)
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    return path


async def _collect(agen):
    return [chunk async for chunk in agen]


def _events(chunks):
    out = []
    for c in chunks:
        assert c.startswith("data: ")
        payload = c[len("data: "):].strip()
        out.append(None if payload == "[DONE]" else json.loads(payload))
    return out


@pytest.mark.asyncio
async def test_no_live_run_and_still_running_outbox_row_emits_an_uncertain_event(outbox_dir):
    """No live run for this session (agent_runs._RUNS has nothing for it —
    same shape as a process restart) + the outbox row never reached a
    terminal status: the caller must be told the outcome is unknown, not
    handed a bare [DONE] it could read as a clean finish."""
    assert "no-such-session-ever" not in agent_runs._RUNS
    chat_outbox.record_intent(owner="alice", session_id="no-such-session-ever",
                               client_message_id="cid-x")
    chat_outbox.mark_running(owner="alice", session_id="no-such-session-ever",
                              client_message_id="cid-x", run_id="gone")

    chunks = await _collect(
        chat_routes._idempotent_replay_stream(
            "no-such-session-ever", owner="alice", client_message_id="cid-x",
        )
    )
    events = _events(chunks)
    assert events[0]["type"] == "uncertain"
    assert events[0]["status"] == "checking"
    assert events[-1] is None  # the trailing [DONE]


@pytest.mark.asyncio
async def test_no_live_run_but_a_terminal_outbox_row_stays_a_bare_done(outbox_dir):
    """Reverting behavior for the common, non-crash case: a run that simply
    finished and aged out of the reconnect window must NOT be reported as
    uncertain — the turn's own POST already saved the assistant message.
    (Also proves the check re-reads the CURRENT row rather than trusting a
    stale snapshot: the row starts `accepted`, is only marked `finished`
    AFTER that, and the fallback still correctly sees `finished`.)"""
    for cid, terminal in (("cid-fin", "finished"), ("cid-fail", "failed")):
        chat_outbox.record_intent(owner="alice", session_id="no-such-session-ever",
                                   client_message_id=cid)
        chat_outbox.mark_finished(owner="alice", session_id="no-such-session-ever",
                                   client_message_id=cid, status=terminal)
        chunks = await _collect(
            chat_routes._idempotent_replay_stream(
                "no-such-session-ever", owner="alice", client_message_id=cid,
            )
        )
        assert chunks == ["data: [DONE]\n\n"]


@pytest.mark.asyncio
async def test_absent_client_message_id_is_unaffected(outbox_dir):
    """The default (no `client_message_id` passed at all) behaves exactly as
    before this lote — the signature must stay backward compatible for any
    other caller."""
    chunks = await _collect(chat_routes._idempotent_replay_stream("no-such-session-ever"))
    assert chunks == ["data: [DONE]\n\n"]


@pytest.mark.asyncio
async def test_unknown_client_message_id_is_unaffected(outbox_dir):
    """No outbox row at all for this id (e.g. it was already purged): treated
    the same as a settled turn, not as uncertain."""
    chunks = await _collect(
        chat_routes._idempotent_replay_stream(
            "no-such-session-ever", owner="alice", client_message_id="cid-never-recorded",
        )
    )
    assert chunks == ["data: [DONE]\n\n"]


# ---------------------------------------------------------------------------
# POST /api/chat_stream — end to end through the real route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_stream_post_reports_uncertain_when_first_attempt_never_settled(
    monkeypatch, outbox_dir,
):
    """Simulates the crash case directly at the outbox layer: a first
    attempt was accepted and its run named (`mark_running`), but the process
    never got to `mark_finished` -- and, on the "restarted" server, no live
    run exists for this session either. A second POST with the SAME
    client_message_id must reconnect (not start a new turn) and surface the
    uncertain state instead of silently claiming completion."""
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)

    chat_outbox.record_intent(owner="alice", session_id="session-1", client_message_id="cid-crash")
    chat_outbox.mark_running(owner="alice", session_id="session-1",
                              client_message_id="cid-crash", run_id="run-that-is-gone")
    assert "session-1" not in agent_runs._RUNS

    req = _RouteRequest("chat")
    req._form["client_message_id"] = "cid-crash"
    response = await endpoint(req)

    assert response.headers.get("X-Faustus-Idempotent-Replay") == "1"
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else chunk.encode()
    assert b'"type": "uncertain"' in body or b'"type":"uncertain"' in body
    assert b"data: [DONE]" in body
    # No second turn was started.
    assert "chat_called" not in captured and "agent_called" not in captured


# ---------------------------------------------------------------------------
# POST /api/chat (non-streaming) — the JSON reply's own `uncertain` flag
# ---------------------------------------------------------------------------

class _ChatHandler:
    async def handle_memory_command(self, _session, _message):
        return None


class _JsonRequest:
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
        endpoint_url="https://selected.example/v1", model="selected-model",
        headers={"Authorization": "Bearer selected"}, name="test", history=[],
        add_message=added.append,
    )
    session_manager = SimpleNamespace(
        get_session=lambda session_id: session, save_sessions=lambda: None,
    )
    context = SimpleNamespace(
        user="alice", messages=[{"role": "user", "content": "hello"}],
        route_messages=[{"role": "user", "content": "hello"}], preface=[],
        preset=SimpleNamespace(temperature=0.2, max_tokens=128, character_name=None),
        context_length=4096, uprefs={},
    )
    calls = {"model": 0}

    async def fake_build_context(*a, **k):
        return context

    async def fake_llm_call(*a, **k):
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
    return endpoint, calls


@pytest.mark.asyncio
async def test_json_replay_flags_uncertain_while_the_first_attempt_is_still_open(
    monkeypatch, outbox_dir,
):
    endpoint, calls = _chat_endpoint_harness(monkeypatch)
    chat_request = ChatRequest(message="hello", session="session-1")

    # No first real POST at all here -- just the row a first attempt would
    # have left behind before finishing, exactly the state a crash mid-turn
    # leaves it in.
    chat_outbox.record_intent(owner="alice", session_id="session-1", client_message_id="cid-open")

    result = await endpoint(_JsonRequest({"client_message_id": "cid-open"}), chat_request)

    assert result["idempotent_replay"] is True
    assert result["uncertain"] is True
    assert result["status"] == "accepted"
    assert calls["model"] == 0  # no second turn was started


@pytest.mark.asyncio
async def test_json_replay_of_a_finished_turn_is_not_flagged_uncertain(monkeypatch, outbox_dir):
    endpoint, calls = _chat_endpoint_harness(monkeypatch)
    chat_request = ChatRequest(message="hello", session="session-1")

    result1 = await endpoint(_JsonRequest({"client_message_id": "cid-done"}), chat_request)
    assert "uncertain" not in result1  # first send: no replay at all

    result2 = await endpoint(_JsonRequest({"client_message_id": "cid-done"}), chat_request)
    assert result2["idempotent_replay"] is True
    assert result2.get("uncertain") is not True
    assert calls["model"] == 1
