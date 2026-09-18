"""A04 (docs/spec/paridad/, Lote T2): resumable replay by cursor, end to
end through the REAL `GET /api/chat/resume/{session_id}` route, a REAL
`src.agent_runs` detached run started by the REAL `POST /api/chat_stream`
route, and a fake (but call-counted) model stream.

The unit-level mechanics (`agent_runs.subscribe(from_sequence=...)`) are
already covered by `tests/test_obs_agent_runs.py`; the route-level wiring
(cursor threaded through) by `tests/test_obs_chat_resume_route.py` -- but
both mock the piece they are not testing (subscribe is driven directly in
one, mocked out entirely in the other). This test drives neither mock: a
client subscribes (POST /api/chat_stream's own response), is cut off after
k events, and reconnects through the real route with cursor=k while the run
is still `running` -- the resumed stream must show no gap, no semantic
duplicate, exactly one terminal `[DONE]`, and the fake model must have been
called exactly once (resuming never re-invokes it).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_runs, api_version
import routes.chat_routes as chat_routes
from tests.test_foreground_model_routing import _RouteRequest, _chat_stream_endpoint


@pytest.fixture(autouse=True)
def _isolated():
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()


class _ResumeReq:
    def __init__(self, user="alice"):
        self.headers: dict = {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user=user)


def _resume_endpoint():
    """A separate, minimal router: chat_resume only touches
    `_verify_session_owner`/`agent_runs`, both already patched/real by the
    time this is called, so it does not need the same session_manager the
    send endpoint uses."""
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
    )
    return next(r.endpoint for r in router.routes if r.path == "/api/chat/resume/{session_id}")


def _decode(chunk: str):
    body = chunk[len("data: "):].strip()
    return None if body == "[DONE]" else json.loads(body)


@pytest.mark.asyncio
@pytest.mark.acceptance("A04")
async def test_resume_with_cursor_has_no_gap_no_dup_one_terminal_one_model_call(monkeypatch):
    captured = {}
    send_endpoint = _chat_stream_endpoint(monkeypatch, "chat", captured)
    resume_endpoint = _resume_endpoint()

    TOTAL = 8
    CUT_AFTER = 3
    calls = {"model": 0}
    resume_point = asyncio.Event()

    async def _fake_chat_stream(candidates, messages, **kwargs):
        calls["model"] += 1
        for i in range(TOTAL):
            if i == CUT_AFTER:
                # Give the test a chance to read exactly the first CUT_AFTER
                # events and disconnect before any more are produced -- the
                # "cut mid-stream" the acceptance case asks for, not a replay
                # of an already-finished run.
                await resume_point.wait()
            yield f'data: {json.dumps({"delta": f"chunk-{i}"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(chat_routes, "stream_llm_with_fallback", _fake_chat_stream)

    req = _RouteRequest("chat")
    req._form["compare_mode"] = "false"
    send_response = await send_endpoint(req)
    run_id = send_response.headers["X-Faustus-Run-Id"]
    assert send_response.headers[api_version.API_VERSION_HEADER] == api_version.API_VERSION

    live = send_response.body_iterator
    # chat_stream's "chat" mode prefaces the deltas with its own `model_info`
    # event and follows them with a `metrics` event before `[DONE]` -- read
    # exactly CUT_AFTER raw frames off the wire, whatever their type, rather
    # than assuming a 1:1 frame-per-delta shape the acceptance case does not
    # actually promise.
    first_chunks = [await live.__anext__() for _ in range(CUT_AFTER)]
    # "cortar": stop reading -- simulate the client's connection dropping.
    # Starlette does exactly this (calls aclose() on the generator) when a
    # real client disconnects.
    await live.aclose()

    first_events = [_decode(c) for c in first_chunks]
    assert all(e is not None for e in first_events), "cut before any [DONE] reached the client"
    # No gap within what was actually delivered.
    assert [e["sequence"] for e in first_events] == list(range(1, CUT_AFTER + 1))
    cursor = first_events[-1]["sequence"]

    # Reconnect through the REAL route with that cursor while the run is
    # still running (it is blocked in resume_point.wait()).
    assert agent_runs.get_active_run("session-1") is not None
    resume_response = await resume_endpoint(_ResumeReq(), "session-1", cursor=cursor)
    assert resume_response.headers["X-Faustus-Run-Id"] == run_id

    # Let the model produce the rest now that a subscriber is attached again.
    resume_point.set()

    resumed_chunks = [c async for c in resume_response.body_iterator]
    done_markers = [c for c in resumed_chunks if c.strip() == "data: [DONE]"]
    resumed_events = [_decode(c) for c in resumed_chunks if _decode(c) is not None]

    # No gap: sequences pick up immediately after the cursor and are
    # contiguous through to the end (model_info(1) + TOTAL deltas + metrics).
    assert [e["sequence"] for e in resumed_events] == list(range(cursor + 1, TOTAL + 2 + 1))
    # No duplicate: no sequence already delivered before the cut reappears --
    # the only way two deliveries could ever legitimately share a
    # (stream_id, sequence) is a compacted progress tick reusing its slot,
    # which none of model_info/delta/metrics is.
    assert {e["sequence"] for e in resumed_events}.isdisjoint({e["sequence"] for e in first_events})
    assert all(e["stream_id"] == run_id for e in first_events + resumed_events)
    # Every delta chunk the model produced appears in the COMBINED stream
    # exactly once, in order -- nothing lost across the cut, nothing repeated.
    all_events_in_order = sorted(first_events + resumed_events, key=lambda e: e["sequence"])
    deltas_seen = [e["delta"] for e in all_events_in_order if "delta" in e]
    assert deltas_seen == [f"chunk-{i}" for i in range(TOTAL)]
    # Terminal exactly once, and only in the resumed stream (the first
    # connection was cut before the generator ever reached it).
    assert len(done_markers) == 1
    assert not any(c.strip() == "data: [DONE]" for c in first_chunks)
    assert resumed_chunks[-1].strip() == "data: [DONE]"

    for _ in range(100):
        run = agent_runs._RUNS.get("session-1")
        if run is not None and run.status != "running":
            break
        await asyncio.sleep(0.01)
    assert agent_runs._RUNS["session-1"].status == "done"

    # Resuming never re-invoked the model -- it only re-subscribed to the
    # SAME in-flight run.
    assert calls["model"] == 1


@pytest.mark.asyncio
@pytest.mark.acceptance("A04")
async def test_resume_without_an_active_run_is_still_a_404():
    """Compatibility (tests/test_obs_chat_resume_route.py covers this with a
    mock; repeated here against the real route + real agent_runs registry
    with nothing started, so A04's green status does not depend on a mock
    ever agreeing with the real behaviour)."""
    from fastapi import HTTPException
    resume_endpoint = _resume_endpoint()
    with pytest.raises(HTTPException) as exc:
        await resume_endpoint(_ResumeReq(), "no-such-session")
    assert exc.value.status_code == 404
