"""H08: a causal execution ledger that does not depend on the visual replay."""
import asyncio
import json
import sqlite3

import pytest

from src import agent_runs, exec_ledger as xl, tool_execution
from src.agent_tools import ToolBlock
from src.run_causality import bind_run, reset_run
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT


@pytest.fixture(autouse=True)
def ledger(tmp_path):
    xl.set_db_path(str(tmp_path / "ledger.sqlite3"))
    yield tmp_path
    xl.set_db_path(None)


def _impl(monkeypatch, fn):
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fn)


async def _ok(block, **kw):
    return "ok", {"status": "succeeded", "output": "done", "exit_code": 0}


def test_rows_cannot_be_changed_or_deleted():
    xl.call_requested("r1", "s1", "c1", tool="read_file", args_sha256="x")
    path = xl.db_path()
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE exec_events SET kind = 'run_state'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM exec_events")
    conn.close()
    assert len(xl.events("r1")) == 1


@pytest.mark.asyncio
async def test_a_tool_call_leaves_call_attempt_and_result_reference(monkeypatch):
    _impl(monkeypatch, _ok)
    token = bind_run("s1", "run-a")
    try:
        _, res = await tool_execution.execute_tool_block(
            ToolBlock("read_file", '{"path":"a.txt"}'), session_id="s1", owner="alice", call_id="c1",
            security_context=NO_TOOL_SECURITY_CONTEXT)
    finally:
        reset_run(token)
    state = xl.replay("run-a")
    call, = state["calls"]
    assert call["call_id"] == "c1" and call["tool"] == "read_file" and call["state"] == "succeeded"
    attempt, = call["attempts"]
    assert attempt["attempt_id"].startswith("att_")
    assert "attempt_id" not in res  # a read's result is returned as the tool gave it
    assert attempt["result_sha256"] and "done" not in json.dumps(state)  # a reference, not the body
    assert [e["kind"] for e in xl.events("run-a")] == ["call_requested", "attempt_started", "attempt_result"]


@pytest.mark.asyncio
async def test_failure_cancellation_and_unknown_effect_are_kept(monkeypatch):
    async def boom(block, **kw):
        raise ValueError("bad")

    _impl(monkeypatch, boom)
    token = bind_run("s1", "run-b")
    try:
        with pytest.raises(ValueError):
            await tool_execution.execute_tool_block(
                ToolBlock("read_file", "{}"), session_id="s1", call_id="c1", security_context=NO_TOOL_SECURITY_CONTEXT)

        async def cancelled(block, **kw):
            raise asyncio.CancelledError()

        _impl(monkeypatch, cancelled)
        with pytest.raises(asyncio.CancelledError):
            await tool_execution.execute_tool_block(
                ToolBlock("read_file", "{}"), session_id="s1", call_id="c2", security_context=NO_TOOL_SECURITY_CONTEXT)
    finally:
        reset_run(token)
    calls = {c["call_id"]: c for c in xl.replay("run-b")["calls"]}
    assert calls["c1"]["state"] == "failed" and calls["c2"]["state"] == "cancelled"
    assert calls["c2"]["effect_certainty"] == "unknown"
    assert xl.replay("run-b")["unresolved"] == []


@pytest.mark.asyncio
async def test_ledger_survives_the_replay_buffer_being_compacted(monkeypatch, tmp_path):
    """The UI replay buffer is a view: emptying it must not touch the ledger."""
    _impl(monkeypatch, _ok)

    async def gen():
        yield 'data: {"type": "token", "text": "hi"}\n\n'
        await tool_execution.execute_tool_block(
            ToolBlock("read_file", "{}"), session_id="s-run", owner="alice", call_id="c1",
            security_context=NO_TOOL_SECURITY_CONTEXT)
        yield "data: [DONE]\n\n"

    run = agent_runs.start("s-run", gen(), label="t")
    await asyncio.wait_for(run.task, 10)
    run.buffer.clear()
    run.last_key = None
    state = xl.replay(run.run_id)
    assert state["state"] == "done" and [c["call_id"] for c in state["calls"]] == ["c1"]
    assert state["calls"][0]["state"] == "succeeded"
    agent_runs._RUNS.pop("s-run", None)


@pytest.mark.asyncio
async def test_approval_granted_after_a_restart_resumes_the_exact_pending_call(monkeypatch, tmp_path):
    content = '{"x": 1, "y": 2}'
    path = str(tmp_path / "pending.json")
    # --- process 1: the run parks call "call_7" on a card -------------------
    store1 = ToolApprovalStore()
    store1.enable_persistence(path)
    token = bind_run("s1", "run-7")
    try:
        pending = store1.create(
            owner="alice", session_id="s1", origin_run_id="security-ctx-1", tool_name="desktop_click",
            content=content, workspace="", external_untrusted_context_seen=False,
            capabilities=capabilities_for_action("desktop_click", content), call_id="call_7")
    finally:
        reset_run(token)
    xl.run_state("run-7", "s1", "done")
    before = xl.replay("run-7")
    assert before["pending_approvals"] == [pending.approval_id]
    assert before["calls"][0]["call_id"] == "call_7" and before["calls"][0]["state"] == "awaiting_approval"

    # --- restart: new process, new boot id, the store is reloaded from disk ---
    monkeypatch.setattr(xl, "BOOT_ID", "boot-after-restart")
    store2 = ToolApprovalStore()
    assert store2.enable_persistence(path) == 1
    assert [c["call_id"] for c in xl.pending_calls(owner="alice")] == ["call_7"]  # known from the ledger alone
    target = xl.resume_target(pending.approval_id, owner="alice")
    assert (target["run_id"], target["call_id"], target["tool"]) == ("run-7", "call_7", "desktop_click")

    reason, grant = store2.consume_with_reason(pending.approval_id, decision="approve_task", owner="alice", session_id="s1")
    assert reason == "consumed"

    executed = []

    async def handler(block, **kw):
        executed.append(block.content)
        return "clicked", {"status": "succeeded", "output": "ok", "exit_code": 0}

    _impl(monkeypatch, handler)
    # the resumed turn is a NEW run whose call id is the approval id
    token = bind_run("s1", "run-8")
    try:
        await tool_execution.execute_tool_block(
            ToolBlock("desktop_click", content), session_id="s1", owner="alice", call_id=pending.approval_id,
            security_context=ToolRunSecurityContext(), exact_approval=grant)
    finally:
        reset_run(token)

    assert executed == [content]  # exactly once, with the exact arguments
    after = xl.replay("run-7")
    call, = after["calls"]        # still ONE call: it resumed, it did not become a new one
    assert call["call_id"] == "call_7" and call["state"] == "succeeded"
    assert call["approval"]["state"] == "consumed" and call["approval"]["decided_boot"] == "boot-after-restart"
    assert call["resumed"]["args_match"] is True and call["resumed"]["executed_in_run"] == "run-8"
    assert after["pending_approvals"] == []
    assert xl.pending_calls(owner="alice") == []
    # nothing was recorded under the resumed turn's run for this call
    assert xl.replay("run-8")["calls"] == []


@pytest.mark.asyncio
async def test_a_denied_approval_is_recorded_and_the_call_never_runs(monkeypatch):
    content = "{}"
    store = ToolApprovalStore()
    token = bind_run("s1", "run-d")
    try:
        pending = store.create(
            owner="alice", session_id="s1", origin_run_id="x", tool_name="desktop_click", content=content,
            workspace="", external_untrusted_context_seen=False,
            capabilities=capabilities_for_action("desktop_click", content), call_id="call_d")
    finally:
        reset_run(token)
    store.consume_with_reason(pending.approval_id, decision="deny", owner="alice", session_id="s1")
    call, = xl.replay("run-d")["calls"]
    assert call["state"] == "denied" and call["approval"]["state"] == "denied"
    assert [a for a in call["attempts"]] == []


def test_restart_closes_what_the_dead_process_left_open(monkeypatch):
    xl.run_started("run-x", "s1", label="t")
    xl.call_requested("run-x", "s1", "c1", tool="bash", args_sha256="h")
    xl.attempt_started("run-x", "s1", "c1", "att_1")
    monkeypatch.setattr(xl, "BOOT_ID", "boot-2")
    out = xl.recover_after_restart()
    assert out["runs"] == ["run-x"] and out["calls"] == [{"run_id": "run-x", "call_id": "c1"}]
    state = xl.replay("run-x")
    assert state["state"] == "interrupted" and state["reason"] == "process_restart"
    assert state["calls"][0]["state"] == "interrupted" and state["calls"][0]["effect_certainty"] == "unknown"
    assert xl.recover_after_restart() == {"runs": [], "calls": []}  # idempotent


def test_a_lost_write_is_counted_not_raised(monkeypatch):
    def broken():
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(xl, "_db", broken)
    before = xl.summary()["lost_writes"] if False else xl._lost_writes
    assert xl.record("run_state", run_id="r", payload={"state": "done"}) is None
    assert xl._lost_writes == before + 1


def test_disabled_ledger_records_nothing(monkeypatch):
    monkeypatch.setattr(xl, "enabled", lambda: False)
    assert xl.run_state("r", "s", "done") is None
    monkeypatch.setattr(xl, "enabled", lambda: True)
    assert xl.events("r") == []


def test_purge_session_is_the_only_way_rows_leave():
    xl.run_state("r1", "s-gone", "done")
    xl.run_state("r2", "s-kept", "done")
    assert xl.purge_session("s-gone") == 1
    assert xl.events("r1") == [] and len(xl.events("r2")) == 1
    conn = sqlite3.connect(xl.db_path())
    with pytest.raises(sqlite3.DatabaseError):  # the trigger is back
        conn.execute("DELETE FROM exec_events")
    conn.close()
