"""No synthetic email dispatch before the selected tracked path persists intent."""
import asyncio
import hashlib
import json

import pytest

from src import agent_runs, tool_execution
from src.agent_tools import ToolBlock
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT
from src.run_causality import bind_run, reset_run


@pytest.fixture
def tracked_run(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    run = agent_runs._Run()
    run.log = agent_runs._RunLog("h03", run)
    monkeypatch.setattr(agent_runs, "_RUNS", {"h03": run})
    token = bind_run("h03", run.run_id, _effect_recorder=agent_runs._EffectRecorder(run))
    try:
        yield run
    finally:
        reset_run(token)
        if run.log:
            run.log.finish("done")


async def invoke(name="send_email", content='{"to":"test@example.invalid","body":"secret draft"}'):
    return await tool_execution.execute_tool_block(
        ToolBlock(name, content), session_id="h03", owner="alice", call_id="send-1",
        security_context=NO_TOOL_SECURITY_CONTEXT)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["send_email", "reply_to_email", "mcp__email__send_email", "mcp__email__reply_to_email"])
async def test_intent_is_synced_before_dispatch_and_before_subscriber_fanout(tracked_run, monkeypatch, name):
    run = tracked_run
    queue = asyncio.Queue()
    run.subscribers.add(queue)
    synced = []
    original_fsync = agent_runs.os.fsync

    def sync(fd):
        assert run.buffer == [] and queue.empty()
        original_fsync(fd)
        synced.append(fd)

    monkeypatch.setattr(agent_runs.os, "fsync", sync)
    received = []

    async def receiver(*args, **kwargs):
        assert len(synced) == 1
        with open(run.log.path, encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle]
        event = json.loads(rows[-1]["ev"][6:])
        assert event["state"] == "pending" and event["call_id"] == "send-1"
        assert event["intent"]["owner"] == "alice"
        assert event["intent"]["arguments_sha256"] == hashlib.sha256(b'{"body":"secret draft"}').hexdigest()
        assert "secret draft" not in json.dumps(event)
        received.append(event["call_id"])
        return "sent", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", receiver)
    _, result = await invoke(name, '{"body":"secret draft"}')
    assert result["status"] == "succeeded" and received == ["send-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["fsync", "closed", "missing"])
async def test_store_failure_prevents_dispatch_and_pending_fanout(tracked_run, monkeypatch, failure):
    run = tracked_run
    queue = asyncio.Queue()
    run.subscribers.add(queue)
    received = []

    async def receiver(*args, **kwargs):
        received.append(True)
        return "sent", {}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", receiver)
    if failure == "fsync":
        def fail(fd):
            raise OSError("disk unavailable")
        monkeypatch.setattr(agent_runs.os, "fsync", fail)
    elif failure == "closed":
        run.log.orphan()
    else:
        run.log.finish("done")
        run.log = None
    _, result = await invoke()
    assert result["error_code"] == "EFFECT_INTENT_NOT_PERSISTED"
    assert result["effect_not_dispatched"] is True
    assert received == [] and run.buffer == [] and queue.empty()


@pytest.mark.asyncio
async def test_foreground_email_without_detached_run_keeps_existing_dispatch(monkeypatch):
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    received = []
    async def receiver(*args, **kwargs):
        received.append(True)
        return "sent", {"status": "succeeded"}
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", receiver)
    assert (await invoke())[1]["status"] == "succeeded"
    assert received == [True]


@pytest.mark.asyncio
async def test_read_remains_available_without_a_working_log(tracked_run, monkeypatch):
    tracked_run.log.orphan()
    async def reader(*args, **kwargs):
        return "read", {"output": "contents"}
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", reader)
    assert (await invoke("read_file"))[1]["output"] == "contents"


@pytest.mark.asyncio
async def test_capability_lookup_failure_does_not_bypass_selected_email_gate(tracked_run, monkeypatch):
    tracked_run.log.orphan()
    def unavailable(*args, **kwargs):
        raise RuntimeError("capabilities unavailable")
    monkeypatch.setattr(tool_execution, "capabilities_for_action", unavailable)
    async def forbidden(*args, **kwargs):
        pytest.fail("email must not dispatch without durable intent")
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", forbidden)
    assert (await invoke())[1]["effect_not_dispatched"] is True
