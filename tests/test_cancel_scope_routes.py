"""TASK-04/QA-12: POST /api/chat/stop's optional `scope` and the new
POST /api/chat/pause.

* No `scope` (or an unrecognised one) is the ORIGINAL Stop contract, byte for
  byte -- every caller from before this scope existed keeps working.
* `scope="generation"` (== POST /api/chat/pause) does NOT cancel the run's
  task -- it only sets the pause flag `stream_agent_loop`'s `pending_pause`
  consumes at the next safe point (see tests/test_steer_routes.py for that
  side of it).
* `scope="task"`/`"work"` cancel the run for real, propagate to every
  delegate_agents worker the turn started, cancel any question this turn
  still has `open` (QA-14: a late answer must not wake a cancelled turn),
  and (scope="work" only) this session's own background jobs. The response
  carries `cancelled_at`, `what_ran_before` and `cleanup`.

Revert the `_scope` branch in routes/chat_routes.py's `chat_stop` (fold it
back to always calling `agent_runs.stop`) to see every scoped test below
fail; revert the `agent_runs.request_pause`/`take_pause_request` pair in
src/agent_runs.py to see the pause tests fail specifically.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from src import agent_runs, question_store


# ── plumbing (mirrors tests/test_subagent_steer_routes.py) ─────────────────

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
def qstore(tmp_path, monkeypatch):
    path = tmp_path / "questions.db"
    monkeypatch.setattr(question_store, "default_path", lambda: path)
    return question_store.Store(path)


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


async def _never_ending(sid, started: asyncio.Event, published_tool_start=True):
    started.set()
    if published_tool_start:
        yield 'data: {"type": "tool_start", "tool": "bash", "command": "sleep 1", "round": 1, "call_id": "c1"}\n\n'
    try:
        while True:
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        raise


async def _start_run(sid, published_tool_start=True):
    started = asyncio.Event()
    run = agent_runs.start(sid, _never_ending(sid, started, published_tool_start), label=sid)
    await asyncio.wait_for(started.wait(), 2)
    assert agent_runs.is_active(sid)
    return run


async def _wait_done(run, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run.task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run never finished")


# ── no scope: the original contract ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_stop_without_scope_is_the_original_contract(routes):
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run = await _start_run("s1")
    out = await stop(_Req(body=None, run_id=run.run_id), "s1")
    assert out == {"stopped": True}
    await _wait_done(run)
    assert run.status == "stopped"


@pytest.mark.asyncio
async def test_stop_without_a_recognised_scope_also_falls_back(routes):
    """An unknown scope string behaves exactly like no scope at all -- future
    callers get a graceful fallback, not a 500."""
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run = await _start_run("s1b")
    out = await stop(_Req(body={"scope": "nonsense"}, run_id=run.run_id), "s1b")
    assert out == {"stopped": True}


# ── scope="generation" / POST /api/chat/pause ──────────────────────────────

@pytest.mark.asyncio
async def test_stop_scope_generation_pauses_without_cancelling(routes):
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run = await _start_run("s2")
    out = await stop(_Req(body={"scope": "generation"}, run_id=run.run_id), "s2")
    assert out == {"scope": "generation", "stopped": False, "paused": True}
    # The task itself is untouched -- pausing is cooperative, consumed by the
    # loop's own pending_pause, not a cancellation from outside.
    await asyncio.sleep(0.02)
    assert not run.task.done()
    assert agent_runs.is_active("s2")
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_route(routes):
    pause = routes("/api/chat/pause/{session_id}", "POST")
    run = await _start_run("s3")
    out = await pause(_Req(run_id=run.run_id), "s3")
    assert out == {"paused": True}
    assert agent_runs.take_pause_request("s3") is True
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_route_fails_closed_on_a_stale_run_id(routes):
    pause = routes("/api/chat/pause/{session_id}", "POST")
    run = await _start_run("s4")
    out = await pause(_Req(run_id="not-the-real-one"), "s4")
    assert out == {"paused": False}
    assert agent_runs.take_pause_request("s4") is False
    run.task.cancel()
    await asyncio.gather(run.task, return_exceptions=True)


# ── scope="task" / "work": real cancellation + propagation ────────────────

@pytest.mark.asyncio
async def test_stop_scope_task_cancels_and_reports_cleanup(routes, qstore):
    from src.agent_tools import subagent_tools as st
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run = await _start_run("parent1")

    # A live delegate_agents worker of this turn.
    worker = st.SubagentRun(0, {"name": "w", "instruction": "x"})
    worker.session_id = "kid1"
    worker.parent_session_id = "parent1"

    async def _worker_forever():
        await asyncio.Event().wait()
    worker_task = asyncio.get_running_loop().create_task(_worker_forever())
    st._ACTIVE_WORKERS["kid1"] = worker_task
    st._WORKER_RUNS["kid1"] = worker
    try:
        # An ask_user question this turn is still waiting on.
        q = qstore.open("Which file?", session_id="parent1", owner="alice")

        out = await stop(_Req(body={"scope": "task"}, run_id=run.run_id), "parent1")

        assert out["scope"] == "task"
        assert out["stopped"] is True
        assert isinstance(out["cancelled_at"], float) and out["cancelled_at"] > 0
        assert out["what_ran_before"] == ["bash"]
        assert out["cleanup"]["subagents_stopped"] == ["kid1"]
        assert out["cleanup"]["questions_cancelled"] == [q["question_id"]]
        assert "bg_jobs_cancelled" not in out["cleanup"]

        await _wait_done(run)
        assert run.status == "stopped"
        assert worker_task.cancelled() or worker_task.done()

        # QA-14: a late answer to the now-cancelled question wakes nothing.
        resolved = qstore.resolve(q["question_id"], "b.py")
        assert resolved["ok"] is False
        assert resolved["reason"] == "cancelled"
    finally:
        st._ACTIVE_WORKERS.pop("kid1", None)
        st._WORKER_RUNS.pop("kid1", None)
        if not worker_task.done():
            worker_task.cancel()


@pytest.mark.asyncio
async def test_stop_scope_work_also_cancels_bg_jobs(routes, monkeypatch):
    from src import bg_jobs
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run = await _start_run("parent2")

    killed = []

    def _fake_cancel_for_session(sid):
        killed.append(sid)
        return [{"id": "job1", "status": "failed", "killed": True}]
    monkeypatch.setattr(bg_jobs, "cancel_for_session", _fake_cancel_for_session)

    out = await stop(_Req(body={"scope": "work"}, run_id=run.run_id), "parent2")
    assert out["scope"] == "work"
    assert out["cleanup"]["bg_jobs_cancelled"] == ["job1"]
    assert killed == ["parent2"]
    await _wait_done(run)


@pytest.mark.asyncio
async def test_stop_scope_task_never_touches_another_sessions_question(routes, qstore):
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run_a = await _start_run("sess-a")
    await _start_run("sess-b")
    q_b = qstore.open("Unrelated question", session_id="sess-b", owner="alice")

    out = await stop(_Req(body={"scope": "task"}, run_id=run_a.run_id), "sess-a")
    assert out["cleanup"]["questions_cancelled"] == []
    still_open = qstore.get(q_b["question_id"])
    assert still_open["status"] == "open"

    run_b = agent_runs.get_active_run("sess-b")
    if run_b is not None and not run_b.task.done():
        run_b.task.cancel()
        await asyncio.gather(run_b.task, return_exceptions=True)
