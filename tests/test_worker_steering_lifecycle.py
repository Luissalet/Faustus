"""Worker instruction admission per attempt, using no models or external IO."""
import asyncio
import json

import pytest

from src import agent_loop, ai_interaction, agent_runs
from src.agent_tools import subagent_tools as st


@pytest.fixture
def worker(monkeypatch):
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: None)
    monkeypatch.setattr(agent_runs, "mark_busy", lambda *a: None)
    monkeypatch.setattr(agent_runs, "clear_busy", lambda *a: None)
    monkeypatch.setattr(st, "_ACTIVE_WORKERS", {})
    monkeypatch.setattr(st, "_WORKER_RUNS", {})
    return st.SubagentRun(0, {"name": "fixture", "instruction": "Synthetic task"})


async def attempt(worker, emit):
    await st._run_subagent(worker, endpoint_url="http://unused", model="fixture", headers=None,
        owner=None, workspace=None, workspace_roots=None, max_rounds=1, shared_context="",
        parent_session_id=None, emit=emit, save_transcript=False)


def register(worker):
    st._ACTIVE_WORKERS[worker.session_id] = asyncio.current_task()
    st._WORKER_RUNS[worker.session_id] = worker


@pytest.mark.asyncio
async def test_terminal_fanout_rejects_steer_while_task_still_alive(worker, monkeypatch):
    async def stream(*a, **kw):
        register(worker)
        yield "data: [DONE]\n\n"

    async def emit(event):
        if event["event"] == "done":
            assert worker.finished is not None
            assert not asyncio.current_task().done()
            assert st.steer_worker(worker.session_id, "late instruction") is False

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert worker.steer_queue == [] and not worker.accepts_steers


@pytest.mark.asyncio
async def test_live_steer_uses_real_queue_and_message_injection(worker, monkeypatch):
    events = []

    async def stream(*a, **kw):
        register(worker)
        assert st.steer_worker(worker.session_id, "  Inspect fixture  ") is True
        pending = kw["pending_user_messages"]()
        messages = []
        injected, *_ = agent_loop._apply_steers_to_messages(messages, pending)
        assert messages[-1] == {"role": "user", "content": "Inspect fixture"}
        assert st.pending_steers(worker.session_id) == []
        for event in injected:
            yield "data: " + json.dumps(event) + "\n\n"

    async def emit(event):
        events.append(event)

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert worker.steered == 1 and any(e["event"] == "steer" for e in events)
    assert not worker.accepts_steers


@pytest.mark.asyncio
async def test_retry_reopens_gate_even_with_previous_finished_timestamp(worker, monkeypatch):
    attempts = []

    async def stream(*a, **kw):
        register(worker)
        attempts.append(worker.finished)
        assert worker.accepts_steers
        assert st.steer_worker(worker.session_id, "Retry instruction")
        assert kw["pending_user_messages"]()[0]["text"] == "Retry instruction"
        yield "data: [DONE]\n\n"

    async def emit(event):
        if event["event"] == "done":
            assert not worker.accepts_steers

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    await attempt(worker, emit)
    assert attempts[0] is None and attempts[1] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_error_or_cancellation_closes_gate(worker, monkeypatch, failure):
    async def stream(*a, **kw):
        register(worker)
        assert worker.accepts_steers
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("synthetic failure")
        yield "unreachable"

    async def emit(event):
        if event["event"] in ("error", "done"):
            assert not worker.accepts_steers
            assert not st.steer_worker(worker.session_id, "late")

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await attempt(worker, emit)
    else:
        await attempt(worker, emit)
    assert not worker.accepts_steers


@pytest.mark.asyncio
async def test_registered_but_unstarted_worker_rejects_steer(worker):
    worker.session_id = "queued-fixture"
    register(worker)
    assert not st.steer_worker(worker.session_id, "instruction")
    assert worker.steer_queue == []


@pytest.mark.asyncio
async def test_closing_gate_preserves_previously_queued_instruction_for_retry(worker, monkeypatch):
    attempts = []

    async def stream(*a, **kw):
        register(worker)
        attempts.append(True)
        if len(attempts) == 1:
            assert st.steer_worker(worker.session_id, "Previously queued")
        else:
            assert kw["pending_user_messages"]() == [{"text": "Previously queued", "source": "user"}]
        yield "data: [DONE]\n\n"

    async def emit(event):
        pass

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert worker.steered == 0 and len(worker.steer_queue) == 1
    await attempt(worker, emit)
    assert worker.steer_queue == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [
    'data: {"type":"agent_terminal","data":{}}\n\n',
    'event: error\ndata: {"error":"fixture terminal"}\n\n',
])
async def test_terminal_stream_event_closes_before_error_fanout_and_retry_reopens(worker, monkeypatch, terminal):
    attempts = []

    async def stream(*a, **kw):
        register(worker)
        attempts.append(True)
        assert worker.accepts_steers
        if len(attempts) == 1:
            yield terminal
        else:
            assert st.steer_worker(worker.session_id, "Retry input")
            assert kw["pending_user_messages"]()[0]["text"] == "Retry input"
            yield "data: [DONE]\n\n"

    async def emit(event):
        if event["event"] == "error":
            assert not asyncio.current_task().done()
            assert not worker.accepts_steers
            assert not st.steer_worker(worker.session_id, "Late error input")

    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert worker.steer_queue == []
    await attempt(worker, emit)
    assert len(attempts) == 2 and not worker.accepts_steers
