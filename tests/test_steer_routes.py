"""UX-04: POST /api/chat/steer/{session_id} (mode="steer" | "queue") for the
MAIN session turn, and the loop-side safe points both it and pause rely on:

* `mode="steer"` (default) queues text that `stream_agent_loop`'s
  `pending_user_messages` injects as a user message at the next round
  boundary -- mirrors src/agent_tools/subagent_tools.py's steer_worker, just
  for the main run instead of a delegate_agents worker.
* `mode="queue"` ("Enviar despues") NEVER touches the live turn's steer
  queue -- it is held in `send_after_queue` for delivery once the turn ends.
* `stream_agent_loop(pending_pause=...)`, checked at the SAME safe point as
  steering: a True return ends the turn right there (a `paused` event,
  `stop_reason == "paused"`), same shape as an ask_user break -- nothing is
  fed back, no further round starts.

Revert the `queue_steer`/`take_steers`/`queue_send_after` additions in
src/agent_runs.py to see the route tests fail; revert the `pending_pause`
block in src/agent_loop.py (right after the steering injection, before
`round_response = ""`) to see `test_loop_pauses_at_the_next_safe_point`
fail -- round 2 would run instead of the turn stopping at round 1.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import agent_runs


# ── route plumbing ──────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body=None, run_id=None, user="alice"):
        self.headers = {"X-Odysseus-Run-Id": run_id} if run_id else {}
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.state = SimpleNamespace(current_user=user)
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture(autouse=True)
def _isolated_runs():
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()


@pytest.fixture
def routes(monkeypatch):
    from routes import chat_routes
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *a, **kw: None)
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "alice")
    router = chat_routes.setup_chat_routes(
        SimpleNamespace(sessions={}), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(), SimpleNamespace(),
    )

    def _route(path, method):
        for r in reversed(router.routes):
            if r.path == path and method in getattr(r, "methods", set()):
                return r.endpoint
        raise AssertionError(f"route not found: {method} {path}")
    return _route


async def _never_ending(started: asyncio.Event):
    started.set()
    try:
        while True:
            await asyncio.sleep(0.01)
            yield "data: {}\n\n"
    except asyncio.CancelledError:
        raise


async def _start_run(sid):
    started = asyncio.Event()
    run = agent_runs.start(sid, _never_ending(started), label=sid)
    await asyncio.wait_for(started.wait(), 2)
    return run


# ── POST /api/chat/steer/{sid} ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_steer_route_default_mode_queues_for_the_live_turn(routes):
    steer = routes("/api/chat/steer/{session_id}", "POST")
    run = await _start_run("live1")
    out = await steer(_Req(body={"text": "  focus on the tests first  "}, run_id=run.run_id), "live1")
    assert out == {"ok": True, "mode": "steer"}
    assert agent_runs.take_steers("live1") == [{"text": "focus on the tests first", "source": "user"}]
    assert agent_runs.take_steers("live1") == []  # drained once
    assert agent_runs.take_send_after("live1") == []  # never touched the OTHER queue
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_steer_route_queue_mode_never_touches_the_live_steer_queue(routes):
    steer = routes("/api/chat/steer/{session_id}", "POST")
    run = await _start_run("live2")
    out = await steer(_Req(body={"text": "send this later", "mode": "queue"}, run_id=run.run_id), "live2")
    assert out == {"ok": True, "mode": "queue"}
    assert agent_runs.take_steers("live2") == []
    assert agent_runs.take_send_after("live2") == [{"text": "send this later", "source": "user"}]
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_steer_route_404s_when_nothing_is_running(routes):
    from fastapi import HTTPException
    steer = routes("/api/chat/steer/{session_id}", "POST")
    with pytest.raises(HTTPException) as exc:
        await steer(_Req(body={"text": "hello"}), "ghost")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_steer_route_rejects_empty_text(routes):
    from fastapi import HTTPException
    steer = routes("/api/chat/steer/{session_id}", "POST")
    run = await _start_run("live3")
    for body in ({"text": "   "}, {}, None):
        with pytest.raises(HTTPException) as exc:
            await steer(_Req(body=body, run_id=run.run_id), "live3")
        assert exc.value.status_code == 400
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_steer_route_fails_closed_on_a_stale_run_id(routes):
    steer = routes("/api/chat/steer/{session_id}", "POST")
    run = await _start_run("live4")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await steer(_Req(body={"text": "hi"}, run_id="not-the-real-one"), "live4")
    assert exc.value.status_code == 404
    assert agent_runs.take_steers("live4") == []
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


# ── the loop hook (mirrors tests/test_subagent_steer_routes.py) ────────────

def test_stream_agent_loop_accepts_pending_pause():
    import inspect
    import src.agent_loop as al
    p = inspect.signature(al.stream_agent_loop).parameters["pending_pause"]
    assert p.default is None


def test_loop_pauses_at_the_next_safe_point(tmp_path, monkeypatch):
    """Round 1 runs a tool; pause is requested while it runs; round 2 must
    NEVER start -- the turn ends right there with a `paused` event."""
    import src.agent_loop as al
    from tests.test_agent_harness_loop import _collect, _events, _patch_common

    _patch_common(monkeypatch, tool_result={"output": "ok", "exit_code": 0})
    seen_requests = []
    pause_requested = {"now": False}

    async def _exec_and_request_pause(block, *a, **k):
        pause_requested["now"] = True
        return (block.tool_type, {"output": "x = 1", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _exec_and_request_pause, raising=False)

    call = '```read_file\n{"path": "a.py"}\n```'

    async def _fake_stream(_candidates, messages, **kwargs):
        seen_requests.append([dict(m) for m in messages])
        yield f'data: {json.dumps({"delta": call})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Read a.py, I'll pause you"}],
        max_rounds=4, relevant_tools={"read_file"}, workspace=str(tmp_path),
        pending_pause=lambda: pause_requested["now"],
    )
    events = _events(_collect(gen))
    paused = [e for e in events if e.get("type") == "paused"]
    assert len(paused) == 1 and paused[0]["round"] == 2
    # Only round 1's request was ever made -- the turn stopped BEFORE asking
    # the model anything for round 2.
    assert len(seen_requests) == 1


def test_loop_without_a_pause_request_runs_normally(tmp_path, monkeypatch):
    """pending_pause returning False every time changes nothing -- the turn
    completes exactly as it would with no pending_pause at all."""
    import src.agent_loop as al
    from tests.test_agent_harness_loop import _collect, _events, _patch_common, _scripted_stream

    _patch_common(monkeypatch, tool_result={"output": "ok", "exit_code": 0})
    _scripted_stream(monkeypatch, [
        ('```read_file\n{"path": "a.py"}\n```', "tool_calls"),
        ("Done. No files were changed.", "stop"),
    ])
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Read a.py"}],
        max_rounds=4, relevant_tools={"read_file"}, workspace=str(tmp_path),
        pending_pause=lambda: False,
    )
    events = _events(_collect(gen))
    assert not [e for e in events if e.get("type") == "paused"]
    assert any(e.get("delta") and "Done" in e["delta"] for e in events)


def test_drain_marks_a_paused_run_waiting_user(monkeypatch):
    """agent_runs._drain tells a `paused` end apart from an ordinary `done`
    purely from the SSE substring the loop emits -- no coupling to
    agent_loop.py's internals beyond that one event shape."""
    agent_runs._RUNS.clear()

    async def _paused_gen():
        yield 'data: {"delta": "partial"}\n\n'
        yield 'data: {"type": "paused", "round": 2}\n\n'

    async def _run():
        run = agent_runs.start("pausing-sess", _paused_gen(), label="pausing-sess")
        for _ in range(200):
            if run.task.done():
                break
            await asyncio.sleep(0.01)
        assert run.task.done()
        assert run.status == "waiting_user"

    asyncio.run(_run())
    agent_runs._RUNS.clear()
