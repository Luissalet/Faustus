"""Real drain → dispatcher → worker history causality, no model calls."""
import asyncio
import json

import pytest

from src import agent_runs, tool_execution
from src.agent_tools import subagent_tools as st
from src.run_causality import bind_run, capture_call, reset_run
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_schemas import function_call_to_tool_block
from tests.test_subagent_causal_identity import history, reload_metadata


@pytest.fixture
def dispatch(history, monkeypatch):
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(agent_runs, "_schedule_evict", lambda *a: None)
    monkeypatch.setattr(agent_runs, "_record_finished_marker", lambda *a: None)
    seen = []

    async def worker(run, **kw):
        seen.append(run)
        run.session_id, run.parent_session_id = run.id, kw["parent_session_id"]
        history.create_session(session_id=run.id, name=run.name, endpoint_url="http://unused", model="fixture")
        run.text, run.stop_reason = "Result", "complete"

    monkeypatch.setattr(st, "_run_subagent", worker)

    async def call(session="parent", call_id="native-call", legacy_run="legacy-session"):
        block = function_call_to_tool_block("delegate_agents", json.dumps({
            "tasks": [{"name": "worker", "instruction": "Inspect fixture"}], "reviewer": False}))
        _, result = await tool_execution.execute_tool_block(block, session_id=session,
            owner="fixture", call_id=call_id, security_context=ToolRunSecurityContext(),
            turn_options={"run_id": legacy_run})
        assert result.get("exit_code") == 0, result
        return next(r for r in seen if r.id == result["subagents"][0]["id"])

    return call


@pytest.mark.asyncio
async def test_old_caller_uses_drain_snapshot_not_replacement_registry(dispatch, monkeypatch):
    original = agent_runs._Run()
    replacement = agent_runs._Run()

    async def events():
        monkeypatch.setitem(agent_runs._RUNS, "parent", replacement)
        worker = await dispatch(legacy_run="invented-legacy")
        metadata = reload_metadata(worker.id)[-1]
        assert metadata["parent_run_id"] == original.run_id
        assert metadata["parent_call_id"] == "native-call"
        assert metadata["parent_run_id"] != replacement.run_id
        yield "data: [DONE]\n\n"

    await agent_runs._drain("parent", original, events())
    assert original.status == "done"
    assert capture_call("parent", None).run_id is None


@pytest.mark.asyncio
async def test_concurrent_drains_with_same_session_do_not_cross_attribute(dispatch):
    ready = asyncio.Event()
    arrived = []
    runs = [agent_runs._Run(), agent_runs._Run()]

    async def events(run, cid):
        arrived.append(cid)
        if len(arrived) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        worker = await dispatch(call_id=cid)
        metadata = reload_metadata(worker.id)[-1]
        assert (metadata["parent_run_id"], metadata["parent_call_id"]) == (run.run_id, cid)
        yield "data: [DONE]\n\n"

    await asyncio.gather(*(agent_runs._drain("parent", run, events(run, f"native-{i}"))
                           for i, run in enumerate(runs)))
    assert all(run.status == "done" for run in runs)
    assert capture_call("parent", None).run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "cancel"])
async def test_drain_resets_origin_on_error_and_cancel(dispatch, failure):
    outer = bind_run("outer", "outer-run")
    run = agent_runs._Run()

    async def events():
        assert capture_call("parent", None).run_id == run.run_id
        if failure == "error":
            raise RuntimeError("fixture error")
        raise asyncio.CancelledError()
        yield "unreachable"

    try:
        await agent_runs._drain("parent", run, events())
        assert capture_call("outer", None).run_id == "outer-run"
        assert capture_call("parent", None).run_id is None
    finally:
        reset_run(outer)


@pytest.mark.asyncio
async def test_child_session_cannot_inherit_parent_snapshot(dispatch, history):
    history.create_session(session_id="child", name="Child", endpoint_url="http://unused", model="fixture")
    token = bind_run("parent", "real-parent-run")
    try:
        worker = await dispatch(session="child", call_id="child-native")
        meta = reload_metadata(worker.id)[-1]
        assert meta["parent_run_id"] is None
        assert meta["parent_call_id"] == "child-native"
    finally:
        reset_run(token)


@pytest.mark.asyncio
async def test_no_snapshot_does_not_invent_run_from_legacy_or_registry(dispatch, monkeypatch):
    monkeypatch.setitem(agent_runs._RUNS, "parent", agent_runs._Run())
    worker = await dispatch(call_id=None, legacy_run="parent")
    meta = reload_metadata(worker.id)[-1]
    assert meta["parent_run_id"] is None and meta["parent_call_id"] is None


@pytest.mark.asyncio
async def test_finalizer_failure_cannot_leak_origin(dispatch, monkeypatch):
    def fail(*args):
        raise RuntimeError("fixture finalizer failure")

    monkeypatch.setattr(agent_runs, "_record_finished_marker", fail)
    token = bind_run("outer", "outer-run")

    async def events():
        yield "data: [DONE]\n\n"

    try:
        with pytest.raises(RuntimeError, match="finalizer failure"):
            await agent_runs._drain("parent", agent_runs._Run(), events())
        assert capture_call("outer", None).run_id == "outer-run"
        assert capture_call("parent", None).run_id is None
    finally:
        reset_run(token)
