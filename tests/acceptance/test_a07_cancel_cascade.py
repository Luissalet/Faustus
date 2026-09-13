"""A07 (docs/spec/trueforge/, Lote T2): `POST /api/chat/stop?scope=task`
cancellation must be TRANSITIVE (parent -> child -> grandchild, not just
direct children) and must retire every pending tool_approval_store card and
cancel every open question_store question anywhere in the stopped
hierarchy, all inside a few seconds, and the response's `cleanup` must say
so.

Real code under test: `routes/chat_routes.py::chat_stop` (the actual route
function), `src.agent_runs` (real registry/`stop`),
`src.agent_tools.subagent_tools.stop_workers_of_parent_by_level` (real BFS),
`src.tool_approvals.ToolApprovalStore` (real store, real
`retire_for_session_ids`), `src.question_store.Store` (real sqlite store).
Faked: the worker TASK BODIES (a coordinator's real generation loop is not
what this case is about) -- one stands in for a live MCP call that only
returns once it observes cancellation (an `asyncio.Event` that is never set
except by CancelledError), matching
`tests/test_cancel_scope_routes.py`'s own `_never_ending` pattern for the
parent turn itself.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from src import agent_runs, question_store
from src.agent_tools import subagent_tools as st
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import capabilities_for_action


# ── plumbing (mirrors tests/test_cancel_scope_routes.py) ───────────────────

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
    st._ACTIVE_WORKERS.clear()
    st._WORKER_RUNS.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    st._ACTIVE_WORKERS.clear()
    st._WORKER_RUNS.clear()


@pytest.fixture
def qstore(tmp_path, monkeypatch):
    path = tmp_path / "questions.db"
    monkeypatch.setattr(question_store, "default_path", lambda: path)
    return question_store.Store(path)


@pytest.fixture
def approval_store(monkeypatch):
    store = ToolApprovalStore()
    import routes.chat_routes as chat_routes
    monkeypatch.setattr(chat_routes, "tool_approval_store", store)
    return store


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


async def _never_ending(sid, started: asyncio.Event):
    started.set()
    yield 'data: {"type": "tool_start", "tool": "bash", "command": "sleep 1", "round": 1, "call_id": "c1"}\n\n'
    try:
        while True:
            await asyncio.sleep(0.01)
    except asyncio.CancelledError:
        raise


async def _start_parent_run(sid):
    started = asyncio.Event()
    run = agent_runs.start(sid, _never_ending(sid, started), label=sid)
    await asyncio.wait_for(started.wait(), 2)
    assert agent_runs.is_active(sid)
    return run


def _register_worker(session_id: str, parent_session_id: str, task: asyncio.Task) -> "st.SubagentRun":
    """Populate the real live-worker registry directly, the same shape
    `_launch`/`one()` populate it with in production -- this is the seam
    `tests/test_cancel_scope_routes.py` already uses to exercise
    `stop_workers_of_parent` without standing up a full `DelegateAgentsTool`
    coordinator loop."""
    run = st.SubagentRun(0, {"name": session_id, "instruction": "x"})
    run.session_id = session_id
    run.parent_session_id = parent_session_id
    st._ACTIVE_WORKERS[session_id] = task
    st._WORKER_RUNS[session_id] = run
    return run


async def _wait_done(run_or_task, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if run_or_task.done() if isinstance(run_or_task, asyncio.Task) else run_or_task.task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("did not reach a terminal state in time")


@pytest.mark.asyncio
@pytest.mark.acceptance("A07")
async def test_scope_task_cascades_through_grandchildren_and_retires_state(
    routes, qstore, approval_store,
):
    stop = routes("/api/chat/stop/{session_id}", "POST")
    parent_run = await _start_parent_run("parent")

    # child: blocked in a fake MCP call that only returns on cancellation.
    child_mcp_saw_cancel = asyncio.Event()
    child_started = asyncio.Event()

    async def _child_worker():
        child_started.set()
        try:
            await asyncio.Event().wait()   # the fake MCP call: never resolves on its own
        except asyncio.CancelledError:
            child_mcp_saw_cancel.set()
            raise
    child_task = asyncio.get_running_loop().create_task(_child_worker())
    _register_worker("child", "parent", child_task)
    await asyncio.wait_for(child_started.wait(), 2)

    # grandchild: delegated BY the child (parent_session_id="child", not
    # "parent") -- this is the transitive hop the original
    # `stop_workers_of_parent` (direct children only) missed. It also has a
    # tool_approval_store card pending, standing in for the delegated
    # approval flow a real nested worker would have gone through.
    grandchild_started = asyncio.Event()

    async def _grandchild_worker():
        grandchild_started.set()
        await asyncio.Event().wait()
    grandchild_task = asyncio.get_running_loop().create_task(_grandchild_worker())
    _register_worker("grandchild", "child", grandchild_task)
    await asyncio.wait_for(grandchild_started.wait(), 2)

    pending_approval = approval_store.create(
        owner="alice", session_id="grandchild", origin_run_id="run-gc",
        tool_name="bash", content="printf pending", workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf pending"),
    )

    # An ask_user question open under the CHILD's session -- not the parent's
    # -- to prove question cancellation also reaches into the hierarchy, not
    # only the top-level turn's own session_id (the pre-A07 behaviour).
    question = qstore.open("Which file?", session_id="child", owner="alice")

    t0 = time.monotonic()
    out = await stop(_Req(body={"scope": "task"}, run_id=parent_run.run_id), "parent")
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0

    # 1) every run in the hierarchy reaches a terminal state.
    await _wait_done(parent_run)
    await _wait_done(child_task)
    await _wait_done(grandchild_task)
    assert parent_run.status == "stopped"
    assert child_task.cancelled() or child_task.done()
    assert grandchild_task.cancelled() or grandchild_task.done()

    # 2) the fake MCP call observed the cancellation (didn't just get abandoned).
    assert child_mcp_saw_cancel.is_set()

    # 3) BFS is transitive: both levels reported, grandchild included.
    assert out["scope"] == "task"
    assert out["stopped"] is True
    assert set(out["cleanup"]["subagents_stopped"]) == {"child", "grandchild"}
    assert out["cleanup"]["workers_stopped"] == [["child"], ["grandchild"]]

    # 4) the pending approval anywhere in the hierarchy is retired.
    assert approval_store.peek(pending_approval.approval_id) is None
    assert out["cleanup"]["approvals_retired"] == [pending_approval.approval_id]

    # 5) the question open under the CHILD (not the parent) is cancelled too.
    assert out["cleanup"]["questions_cancelled"] == [question["question_id"]]
    resolved = qstore.resolve(question["question_id"], "b.py")
    assert resolved["ok"] is False and resolved["reason"] == "cancelled"

    # Back-compat: the pre-A07 shape (`own_process_tree`, `cancelled_at`,
    # `what_ran_before`) is unchanged.
    assert isinstance(out["cancelled_at"], float) and out["cancelled_at"] > 0
    assert out["what_ran_before"] == ["bash"]
    assert "own_process_tree" in out["cleanup"]


@pytest.mark.asyncio
@pytest.mark.acceptance("A07")
async def test_scope_task_does_not_touch_an_unrelated_sibling_hierarchy(
    routes, qstore, approval_store,
):
    """A worker delegated by a DIFFERENT parent, and that parent's own
    approvals/questions, are untouched -- transitivity must not become
    "stop everything"."""
    stop = routes("/api/chat/stop/{session_id}", "POST")
    run_a = await _start_parent_run("sess-a")
    run_b = await _start_parent_run("sess-b")

    other_started = asyncio.Event()

    async def _other_worker():
        other_started.set()
        await asyncio.Event().wait()
    other_task = asyncio.get_running_loop().create_task(_other_worker())
    _register_worker("other-child", "sess-b", other_task)
    await asyncio.wait_for(other_started.wait(), 2)

    other_pending = approval_store.create(
        owner="alice", session_id="sess-b", origin_run_id="run-b",
        tool_name="bash", content="printf b", workspace=None,
        external_untrusted_context_seen=True,
        capabilities=capabilities_for_action("bash", "printf b"),
    )
    q_b = qstore.open("Unrelated question", session_id="sess-b", owner="alice")

    out = await stop(_Req(body={"scope": "task"}, run_id=run_a.run_id), "sess-a")

    assert out["cleanup"]["subagents_stopped"] == []
    assert out["cleanup"]["workers_stopped"] == []
    assert out["cleanup"]["approvals_retired"] == []
    assert out["cleanup"]["questions_cancelled"] == []

    assert not other_task.done()
    assert approval_store.peek(other_pending.approval_id) == other_pending
    still_open = qstore.get(q_b["question_id"])
    assert still_open["status"] == "open"

    other_task.cancel()
    await asyncio.gather(other_task, return_exceptions=True)
    if not run_b.task.done():
        run_b.task.cancel()
        await asyncio.gather(run_b.task, return_exceptions=True)
