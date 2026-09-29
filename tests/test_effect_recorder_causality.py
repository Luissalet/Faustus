"""Drain-bound effect recording with temporary logs and no external effects."""
import asyncio
import json

import pytest

from src import agent_runs, constants, tool_execution
from src.agent_tools import ToolBlock
from src.run_causality import capture_effect_recorder


@pytest.fixture
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(agent_runs, "_schedule_evict", lambda *a: None)
    monkeypatch.setattr(agent_runs, "_record_finished_marker", lambda *a: None)
    made = []

    def make(session="fixture"):
        run = agent_runs._Run()
        run.log = agent_runs._RunLog(session, run)
        run.subscribers.add(asyncio.Queue())
        made.append(run)
        return run

    yield make
    for run in made:
        if run.log:
            run.log.finish("done")


def effects(run):
    return [json.loads(event[6:]) for event in agent_runs._read_log(run.log.path)["events"]
            if event.startswith("data: {") and json.loads(event[6:]).get("type") == "tool_effect"]


async def invoke(session="fixture", cid="native-call"):
    return await tool_execution.execute_tool_block(ToolBlock("send_email", '{"fixture":true}'),
        session_id=session, owner="fixture", call_id=cid,
        security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT)


@pytest.mark.asyncio
async def test_replacement_inside_handler_keeps_both_effects_in_original_run(runs, monkeypatch):
    old, new = runs(), runs()
    agent_runs._RUNS["fixture"] = old
    old_queue, new_queue = next(iter(old.subscribers)), next(iter(new.subscribers))

    async def dummy(*a, **kw):
        assert effects(old)[0]["state"] == "pending"
        assert "_effect_recorder" not in kw
        assert not hasattr(kw["_causal_call"], "_effect_recorder")
        agent_runs._RUNS["fixture"] = new
        old.status = "stopped"
        return "fixture", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dummy)
    outcomes = []

    async def events():
        outcomes.append(await invoke())
        yield "data: [DONE]\n\n"

    await agent_runs._drain("fixture", old, events())
    assert outcomes[0][1]["status"] == "succeeded"
    assert [e["state"] for e in effects(old)] == ["pending", "confirmed"]
    assert {e["idempotency_key"] for e in effects(old)} == {old.run_id + ":native-call"}
    assert not effects(new) and new_queue.empty()
    assert old_queue.qsize() >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stopped", "closed", "missing", "fsync"])
async def test_captured_run_failure_rejects_email_without_adopting_new_run(runs, monkeypatch, failure):
    old, new = runs(), runs()
    calls, outcomes = [], []

    async def dummy(*a, **kw):
        calls.append(True)
        return "fixture", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dummy)

    async def events():
        agent_runs._RUNS["fixture"] = new
        if failure == "stopped":
            old.status = "stopped"
        elif failure == "closed":
            old.log.orphan()
        elif failure == "missing":
            old.log.finish("done")
            old.log = None
        else:
            monkeypatch.setattr(agent_runs.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fixture fsync")))
        outcomes.append(await invoke())
        yield "data: [DONE]\n\n"

    await agent_runs._drain("fixture", old, events())
    assert outcomes[0][1]["error_code"] == "EFFECT_INTENT_NOT_PERSISTED"
    assert calls == [] and not effects(new)
    assert next(iter(new.subscribers)).empty()


@pytest.mark.asyncio
async def test_closed_writer_after_dispatch_does_not_publish_to_replacement(runs, monkeypatch):
    old, new = runs(), runs()

    async def dummy(*a, **kw):
        old.log.orphan()
        agent_runs._RUNS["fixture"] = new
        return "fixture", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dummy)
    outcomes = []

    async def events():
        outcomes.append(await invoke())
        yield "data: [DONE]\n\n"

    await agent_runs._drain("fixture", old, events())
    assert outcomes[0][1]["status"] == "succeeded"
    assert [e["state"] for e in effects(old)] == ["pending"]
    assert not effects(new) and next(iter(new.subscribers)).empty()


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_foreground_or_child_mismatch_never_adopts_registered_run(runs, monkeypatch, child):
    old, new = runs(), runs("child" if child else "fixture")
    session = "child" if child else "fixture"
    agent_runs._RUNS[session] = new
    calls, outcomes = [], []

    async def dummy(*a, **kw):
        calls.append(True)
        return "fixture", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dummy)

    async def events():
        assert capture_effect_recorder("child") is None
        outcomes.append(await invoke(session))
        yield "data: [DONE]\n\n"

    if child:
        await agent_runs._drain("fixture", old, events())
    else:
        outcomes.append(await invoke(session))
    assert calls == [True] and outcomes[0][1]["status"] == "succeeded"
    assert not effects(new) and not effects(old)


@pytest.mark.asyncio
async def test_concurrent_same_session_drains_keep_independent_recorders(runs, monkeypatch):
    old, new = runs(), runs()
    ready = asyncio.Event()
    arrivals = []

    async def dummy(*a, **kw):
        arrivals.append(kw["_causal_call"].run_id)
        if len(arrivals) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 3)
        return "fixture", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dummy)

    async def events(run):
        agent_runs._RUNS["fixture"] = new
        await invoke(cid="same-native-id")
        yield "data: [DONE]\n\n"

    await asyncio.gather(*(agent_runs._drain("fixture", run, events(run)) for run in (old, new)))
    for run in (old, new):
        assert run.status == "done"
        assert [e["state"] for e in effects(run)] == ["pending", "confirmed"]
        assert {e["idempotency_key"] for e in effects(run)} == {run.run_id + ":same-native-id"}


def test_legacy_direct_record_api_remains_available(runs):
    run = runs()
    agent_runs._RUNS["fixture"] = run
    agent_runs.record_tool_effect("fixture", call_id="direct", tool="fixture",
        effect_class="write", state="pending", durable=True)
    assert effects(run)[0]["idempotency_key"] == run.run_id + ":direct"


@pytest.mark.asyncio
async def test_persistence_off_keeps_best_effort_effects_in_original_memory(runs, monkeypatch):
    old, new = runs(), runs()
    old.log.finish("done")
    old.log = None
    outcomes = []

    async def dummy(*a, **kw):
        agent_runs._RUNS["fixture"] = new
        return "fixture", {"status": "succeeded"}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dummy)

    async def events():
        outcomes.append(await tool_execution.execute_tool_block(ToolBlock("bash", "fixture command"),
            session_id="fixture", call_id="shell-call", owner="fixture",
            security_context=tool_execution.NO_TOOL_SECURITY_CONTEXT))
        yield "data: [DONE]\n\n"

    await agent_runs._drain("fixture", old, events())
    assert outcomes[0][1]["status"] == "succeeded"
    payloads = [json.loads(e[6:]) for e in old.buffer if e.startswith("data: {")]
    recorded = [e for e in payloads if e.get("type") == "tool_effect"]
    assert [e["state"] for e in recorded] == ["pending", "confirmed"]
    assert {e["idempotency_key"] for e in recorded} == {old.run_id + ":shell-call"}
    assert new.buffer == [] and not effects(new)
