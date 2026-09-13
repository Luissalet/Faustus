"""A01 (docs/spec/paridad/, Lote T2): turn admission.

Two invariants from the reauditoria (CONTRATO_PARIDAD_1.md):

1. Full request validation completes, and can therefore reject the request
   with a plain 4xx, WITHOUT ever touching a predecessor run already in
   flight -- an invalid POST (malformed body / stale question_id / unknown
   tool_approval_id) must never cancel, replace, or otherwise disturb a
   valid run some other request already started for the same session.
2. Two genuinely CONCURRENT, individually-valid POSTs for the same session
   must not corrupt admission: exactly one run ends up active, and the
   mutating part of admission (persisting the user's message through
   `agent_runs.start()`, `routes/chat_routes.py::_session_admission_lock`)
   is serialized rather than interleaved.

Reuses the SAME harness `tests/test_foreground_model_routing.py` and
`tests/test_chat_idempotency.py` already use to drive the real
`POST /api/chat_stream` route function (`chat_routes.setup_chat_routes`)
with only the model call and chat_outbox/question_store paths faked out --
`src.agent_runs` runs for real. (c) "same client_message_id twice is one
turn" is exhaustively covered by `tests/test_chat_idempotency.py` already;
this file does not duplicate it, only confirms the same guarantee holds
when TWO admissions race for the lock (test 3 below).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.responses import JSONResponse

from src import agent_runs, chat_outbox
import routes.chat_routes as chat_routes
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_LANES", {})
    monkeypatch.setattr(chat_outbox, "default_path", lambda: tmp_path / "chat_outbox.sqlite3")
    from src import question_store
    monkeypatch.setattr(question_store, "default_path", lambda: tmp_path / "questions.sqlite3")
    yield


def _valid_request(mode: str = "chat") -> _RouteRequest:
    """`_RouteRequest` defaults to `compare_mode=true` (a short-lived,
    non-detached pane, see routes/chat_routes.py's own comment on that
    branch) -- every assertion here is about the DETACHED path
    (`agent_runs.start()`/predecessor cancellation), so every request in
    this file turns compare_mode off explicitly."""
    req = _RouteRequest(mode)
    req._form["compare_mode"] = "false"
    return req


async def _drain(response) -> None:
    async for _ in response.body_iterator:
        pass


# ── (a): an invalid POST must never touch a run already in flight ─────────

@pytest.mark.asyncio
@pytest.mark.acceptance("A01")
async def test_invalid_post_never_disturbs_the_run_in_flight(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_chat_stream(candidates, messages, **kwargs):
        started.set()
        await release.wait()
        yield f'data: {json.dumps({"delta": "done"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _slow_chat_stream)

    valid_task = asyncio.create_task(endpoint(_valid_request()))
    await asyncio.wait_for(started.wait(), 2)

    run_before = agent_runs.get_active_run("session-1")
    assert run_before is not None and run_before.status == "running"

    # Invalid #1: a question_id that does not exist -- rejected with a plain
    # 409 JSONResponse, before build_chat_context / agent_runs are ever
    # touched (routes/chat_routes.py's question_id block precedes both).
    stale_question_req = _valid_request()
    stale_question_req._form["question_id"] = "qst_does_not_exist"
    response = await endpoint(stale_question_req)
    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    assert json.loads(bytes(response.body))["reason"] == "not_found"

    assert agent_runs.get_active_run("session-1") is run_before
    assert run_before.status == "running"

    # Invalid #2: an unknown tool_approval_id -- rejected with an HTTPException
    # (409), also entirely before admission.
    from fastapi import HTTPException
    stale_approval_req = _valid_request()
    stale_approval_req._form["tool_approval_id"] = "does-not-exist"
    with pytest.raises(HTTPException) as exc:
        await endpoint(stale_approval_req)
    assert exc.value.status_code == 409

    assert agent_runs.get_active_run("session-1") is run_before
    assert run_before.status == "running"

    # Invalid #3: malformed JSON body (content-type says JSON, body is not an
    # object) -- rejected before even `request.form()` is read.
    class _MalformedJsonRequest(_RouteRequest):
        def __init__(self):
            super().__init__("chat")
            self.headers = {"content-type": "application/json"}

        async def json(self):
            return ["not", "an", "object"]
    with pytest.raises(HTTPException) as exc:
        await endpoint(_MalformedJsonRequest())
    assert exc.value.status_code == 400

    assert agent_runs.get_active_run("session-1") is run_before
    assert run_before.status == "running"

    # The predecessor was never touched by any of the three: it still runs
    # to a normal completion once released.
    release.set()
    valid_response = await valid_task
    await _drain(valid_response)
    for _ in range(100):
        if run_before.status != "running":
            break
        await asyncio.sleep(0.01)
    assert run_before.status == "done"


# ── (b): two concurrent VALID POSTs must not corrupt admission ────────────

@pytest.mark.asyncio
@pytest.mark.acceptance("A01")
async def test_two_concurrent_valid_posts_admission_is_serialized(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)

    original_build_context = chat_routes.build_chat_context
    order: list = []
    counter = {"n": 0}

    async def _tracking_build_context(*args, **kwargs):
        counter["n"] += 1
        tag = counter["n"]
        order.append(("enter", tag))
        # A real await point here (RAG lookup, attachment resolution, ...) is
        # exactly where two admissions could interleave without the lock --
        # force the opportunity so this test actually exercises it rather
        # than passing by accident on scheduling luck.
        await asyncio.sleep(0.03)
        result = await original_build_context(*args, **kwargs)
        order.append(("exit", tag))
        return result
    monkeypatch.setattr(chat_routes, "build_chat_context", _tracking_build_context)

    response_a, response_b = await asyncio.gather(
        endpoint(_valid_request()), endpoint(_valid_request()),
    )
    await asyncio.gather(_drain(response_a), _drain(response_b))

    # Never interleaved: the second admission's "enter" only appears after
    # the first admission's "exit" -- the lock, not scheduling luck, is what
    # the assertion below depends on (the forced sleep above would expose
    # interleaving reliably if the lock were removed).
    assert order == [("enter", 1), ("exit", 1), ("enter", 2), ("exit", 2)]

    # Exactly one run is left registered for the session, and it reaches a
    # normal terminal state -- no corrupted double-registration, no run
    # stuck `running` forever because two admissions raced its bookkeeping.
    for _ in range(100):
        run = agent_runs._RUNS.get("session-1")
        if run is not None and run.status != "running":
            break
        await asyncio.sleep(0.01)
    run = agent_runs._RUNS.get("session-1")
    assert run is not None
    assert run.status == "done"
    assert len(agent_runs._RUNS) == 1


# ── (c): the same guarantee holds when two admissions race for the SAME
#         client_message_id (see tests/test_chat_idempotency.py for the
#         exhaustive, non-racing version of this) ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.acceptance("A01")
async def test_same_client_message_id_races_to_exactly_one_turn(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    calls = {"model": 0}
    real_model_call = chat_routes.stream_llm_with_fallback

    async def _counting_chat_stream(*args, **kwargs):
        calls["model"] += 1
        async for chunk in real_model_call(*args, **kwargs):
            yield chunk
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _counting_chat_stream)

    req_a = _valid_request()
    req_a._form["client_message_id"] = "cid-race"
    req_b = _valid_request()
    req_b._form["client_message_id"] = "cid-race"

    response_a, response_b = await asyncio.gather(endpoint(req_a), endpoint(req_b))
    await asyncio.gather(_drain(response_a), _drain(response_b))

    # chat_outbox's own unique index (not the admission lock) is what makes
    # this safe under true concurrency -- record_intent is called BEFORE the
    # lock (it never mutates the session) and is idempotent on its own. This
    # test exists to confirm that guarantee still holds with the lock in
    # place, not to re-derive it.
    assert calls["model"] == 1
    row = chat_outbox.get(owner="alice", session_id="session-1", client_message_id="cid-race")
    assert row is not None and row["status"] == "finished"
