"""Worker invocation provenance; real dispatcher, native SSE and temp SQLite."""
import asyncio
import json
from uuid import UUID

import pytest
from src import agent_loop as al, tool_execution as te, agent_runs
from src.agent_tools import subagent_tools as st
from src.agent_tools import ToolBlock
from src.run_causality import (bind_run, reset_run, capture_call, capture_effect_recorder,
                               capture_email_intent_requirement)
from src.subagent_permissions import ChildPermissions
from tests.test_subagent_causal_identity import history, reload_metadata
from tests.test_autonomy_budget import _patch_common


async def run_worker(worker, emit=None, **kwargs):
    async def sink(event):
        pass
    await st._run_subagent(worker, endpoint_url="https://synthetic.invalid/v1", model="fixture",
        headers=None, owner="admin", workspace=None, workspace_roots=None, max_rounds=2,
        shared_context="", parent_session_id="parent", emit=emit or sink, **kwargs)


@pytest.fixture
def isolated(history, monkeypatch):
    monkeypatch.setattr(agent_runs, "mark_busy", lambda *a, **k: None)
    monkeypatch.setattr(agent_runs, "clear_busy", lambda *a, **k: None)
    return history


@pytest.mark.asyncio
async def test_native_nested_dispatch_real_worker_stream_and_transcript(isolated, monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: True)
    observed = []
    actual = te.execute_tool_block
    async def dispatch(block, *args, **kwargs):
        origin = capture_call(kwargs.get("session_id"), kwargs.get("call_id"))
        from src.llm_trace import current_run_id
        assert current_run_id() == origin.run_id
        observed.append(origin)
        assert capture_effect_recorder(kwargs.get("session_id")) is None
        return await actual(block, *args, **kwargs)
    monkeypatch.setattr(al, "execute_tool_block", dispatch)
    count = 0
    async def provider(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [{"id": "nested-native-call",
                "name": "delegate_agents", "arguments": json.dumps({"tasks": [{"name": "nested",
                "instruction": "Inspect synthetic fixture only"}], "reviewer": False})}]}) + "\n\n"
            yield 'data: {"type":"finish","finish_reason":"tool_calls"}\n\n'
        else:
            yield 'data: {"delta":"Synthetic inspection complete."}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", provider)
    worker = st.SubagentRun(0, {"name": "worker", "instruction": "Inspect synthetic fixture"})
    worker.permissions = ChildPermissions(may_delegate=True)
    st._bind_causal_identity(worker, "real-parent-run", "root-delegation", "root-call")
    recorder = object()
    token = bind_run("parent", "real-parent-run", _effect_recorder=recorder)
    try:
        await run_worker(worker)
        assert capture_call("parent", None).run_id == "real-parent-run"
        assert capture_effect_recorder("parent") is recorder
    finally:
        reset_run(token)
    assert worker.error is None
    assert observed[0].run_id == worker.invocation_run_ids[0]
    assert observed[0].session_id == worker.session_id
    assert observed[0].call_id == "nested-native-call"
    children = [s for s in isolated.sessions.values() if s.id not in ("parent", worker.session_id)]
    assert len(children) == 1
    nested = reload_metadata(children[0].id)[-1]
    assert nested["parent_run_id"] == worker.invocation_run_ids[0]
    assert nested["parent_call_id"] == "nested-native-call"
    assert nested["parent_session"] == worker.session_id
    assert nested["invocation_run_ids"] and nested["invocation_run_ids"][0] != observed[0].run_id
    stored = reload_metadata(worker.session_id)[-1]
    assert stored["parent_run_id"] == "real-parent-run"
    assert stored["parent_call_id"] == "root-call"
    assert stored["delegation_id"] == "root-delegation"
    assert stored["invocation_run_ids"] == worker.invocation_run_ids


@pytest.mark.asyncio
async def test_retry_uuid_and_options_share_identity_bounded(isolated, monkeypatch):
    seen = []
    async def stream(*args, **kwargs):
        sid = kwargs["session_id"]
        origin = capture_call(sid, "native")
        assert origin.run_id == kwargs["harness_options"]["run_id"]
        UUID(origin.run_id)
        assert origin.run_id not in (sid, worker.id, "model-supplied-run", "real-parent-run")
        assert capture_email_intent_requirement(sid)
        assert capture_effect_recorder(sid) is None
        seen.append(origin.run_id)
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", stream)
    worker = st.SubagentRun(0, {"name": "fixture", "instruction": "Inspect synthetic fixture"})
    st._bind_causal_identity(worker, "real-parent-run", "delegation", "call")
    token = bind_run("parent", "real-parent-run", _effect_recorder=object())
    try:
        for _ in range(10):
            await run_worker(worker, harness_options={"run_id": "model-supplied-run"})
            assert capture_call("parent", None).run_id == "real-parent-run"
    finally:
        reset_run(token)
    assert len(set(seen)) == 10
    assert worker.invocation_run_ids == seen[-8:]
    assert worker.parent_run_id == "real-parent-run" and worker.parent_call_id == "call"
    assert reload_metadata(worker.session_id)[-1]["invocation_run_ids"] == seen[-8:]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["success", "cancel", "stream", "finalizer", "error_emit"])
async def test_reset_precedes_cleanup_even_when_it_fails(isolated, monkeypatch, failure):
    worker = st.SubagentRun(0, {"name": "fixture", "instruction": "Inspect fixture"})
    async def stream(*args, **kwargs):
        assert capture_call(kwargs["session_id"], None).run_id == worker.invocation_run_ids[-1]
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure in ("stream", "error_emit"):
            raise RuntimeError("synthetic stream failure")
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", stream)
    original = st._save_transcript
    def finalize(*args):
        assert capture_call("parent", None).run_id == "outer"
        assert capture_effect_recorder("parent") is recorder
        if failure == "finalizer":
            raise RuntimeError("synthetic finalizer failure")
        original(*args)
    monkeypatch.setattr(st, "_save_transcript", finalize)
    async def emit(event):
        if failure == "error_emit" and event.get("event") == "error":
            raise RuntimeError("synthetic emitter failure")
    recorder = object()
    token = bind_run("parent", "outer", _effect_recorder=recorder)
    try:
        if failure == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await run_worker(worker, emit)
        elif failure in ("finalizer", "error_emit"):
            with pytest.raises(RuntimeError):
                await run_worker(worker, emit)
        else:
            await run_worker(worker, emit)
        assert capture_call("parent", None).run_id == "outer"
        assert capture_call(worker.session_id, None).run_id is None
    finally:
        reset_run(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["send_email", "reply_to_email", "mcp__email__send_email", "mcp__email__reply_to_email"])
@pytest.mark.parametrize("call_id", ["native-email", None])
async def test_worker_email_without_recorder_never_dispatches(isolated, monkeypatch, name, call_id):
    received = []
    async def receiver(*args, **kwargs):
        received.append(True)
        return "sent", {"status": "succeeded"}
    monkeypatch.setattr(te, "_execute_tool_block_impl", receiver)
    async def stream(*args, **kwargs):
        sid = kwargs["session_id"]
        assert capture_effect_recorder(sid) is None
        _, result = await te.execute_tool_block(ToolBlock(name, '{"to":"fixture@example.invalid","body":"synthetic"}'),
            session_id=sid, owner="admin", call_id=call_id, security_context=te.NO_TOOL_SECURITY_CONTEXT)
        assert result["error_code"] == "EFFECT_INTENT_NOT_PERSISTED"
        assert result["effect_not_dispatched"]
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", stream)
    worker = st.SubagentRun(0, {"name": "fixture", "instruction": "Inspect fixture"})
    token = bind_run("parent", "outer", _effect_recorder=object())
    try:
        await run_worker(worker)
    finally:
        reset_run(token)
    assert worker.error is None and received == []


@pytest.mark.asyncio
async def test_concurrent_workers_isolate_origin_and_flags(isolated, monkeypatch):
    origins = []
    ready = asyncio.Event()
    async def stream(*args, **kwargs):
        origins.append((kwargs["session_id"], capture_call(kwargs["session_id"], None).run_id))
        if len(origins) == 2:
            ready.set()
        await ready.wait()
        assert capture_call(kwargs["session_id"], None).run_id == kwargs["harness_options"]["run_id"]
        assert capture_effect_recorder(kwargs["session_id"]) is None
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", stream)
    workers = [st.SubagentRun(i, {"name": str(i), "instruction": "Inspect fixture"}) for i in range(2)]
    await asyncio.gather(*(run_worker(worker) for worker in workers))
    assert all(worker.error is None for worker in workers)
    assert len(set(run_id for _, run_id in origins)) == 2
    assert all(capture_call(sid, None).run_id is None for sid, _ in origins)
