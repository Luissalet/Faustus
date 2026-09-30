"""Durable steering observations, exact parent log, no applied claim/models."""
import asyncio
import json

import pytest
from src import agent_runs, constants, tool_execution
from src.agent_tools import subagent_tools as st
from src.tool_schemas import function_call_to_tool_block
from src.tool_capabilities import ToolRunSecurityContext
from tests.test_subagent_causal_identity import history
from tests.test_worker_steering_lifecycle import worker, attempt, register


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    monkeypatch.setattr(st, "_ACTIVE_WORKERS", {})
    monkeypatch.setattr(st, "_WORKER_RUNS", {})
    monkeypatch.setattr(agent_runs, "_schedule_evict", lambda *a: None)
    monkeypatch.setattr(agent_runs, "_record_finished_marker", lambda *a: None)
    made = []
    def make():
        run = agent_runs._Run()
        run.log = agent_runs._RunLog("parent", run)
        run.subscribers.add(asyncio.Queue())
        made.append(run)
        return run
    yield make
    for run in made:
        if run.log is not None:
            run.log.orphan()


def events(run):
    return [json.loads(event[6:]) for event in agent_runs._read_log(run.log.path)["events"]
        if event.startswith("data: {") and json.loads(event[6:]).get("type") == "steering_receipt"]


def live(recorder=None):
    worker = st.SubagentRun(0, {"name": "fixture", "instruction": "Synthetic"})
    worker.session_id = "child"
    worker.parent_run_id = recorder.run_id if recorder else None
    worker.delegation_id = "delegation"
    worker._steering_recorder = recorder
    worker._steering_attempt_id = "attempt-a"
    worker.accepts_steers = True
    st._ACTIVE_WORKERS[worker.session_id] = asyncio.current_task()
    st._WORKER_RUNS[worker.session_id] = worker
    return worker


@pytest.mark.asyncio
async def test_reopened_journal_has_exact_ids_without_text_and_no_applied(journal):
    run = journal()
    worker = live(agent_runs._EffectRecorder(run))
    queued = st.steer_worker_receipt("child", "sensitive synthetic instruction")
    assert queued["state"] == "queued" and queued["durability"] == "durable"
    assert events(run)[0]["receipt_id"] == queued["receipt_id"]
    worker._steering_attempt_id = "attempt-b"
    assert st.pending_steers("child") == [{"text": "sensitive synthetic instruction", "source": "user"}]
    observed = events(run)
    assert [event["state"] for event in observed] == ["queued", "drained"]
    assert {event["receipt_id"] for event in observed} == {queued["receipt_id"]}
    assert observed[1]["accepted_attempt_run_id"] == "attempt-a"
    assert observed[1]["drained_attempt_run_id"] == "attempt-b"
    assert all(event["parent_run_id"] == run.run_id for event in observed)
    assert "sensitive synthetic" not in json.dumps(observed)
    path = run.log.path
    run.log.orphan()
    assert "drained" in str(agent_runs._read_log(path))  # Reopen needs no live registry.


@pytest.mark.asyncio
async def test_repeated_identical_text_has_distinct_ids_direct_entry_identity(journal):
    run = journal()
    worker = live(agent_runs._EffectRecorder(run))
    first = st.steer_worker_receipt("child", "same")
    second = st.steer_worker_receipt("child", "same")
    assert first["receipt_id"] != second["receipt_id"]
    assert len(st.pending_steers("child")) == 2
    assert [event["receipt_id"] for event in events(run)[2:]] == [first["receipt_id"], second["receipt_id"]]
    assert worker._steering_entry_receipts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no_recorder", "no_log", "closed", "fsync"])
async def test_queue_admission_survives_unknown_durability_without_migration(journal, monkeypatch, failure):
    old, new = journal(), journal()
    recorder = None if failure == "no_recorder" else agent_runs._EffectRecorder(old)
    worker = live(recorder)
    monkeypatch.setitem(agent_runs._RUNS, "parent", new)
    if failure == "no_log":
        old.log.orphan()
        old.log = None
    elif failure == "closed":
        old.log.orphan()
    elif failure == "fsync":
        monkeypatch.setattr(agent_runs.os, "fsync", lambda *a: (_ for _ in ()).throw(OSError("synthetic")))
    receipt = st.steer_worker_receipt("child", "queued anyway")
    assert receipt["durability"] == "unknown" and receipt["state"] == "queued"
    assert st.steer_worker("child", "legacy bool") is True
    assert len(st.pending_steers("child")) == 2
    assert worker._steering_receipts[receipt["receipt_id"]]["state"] == "drained"
    assert worker._steering_receipts[receipt["receipt_id"]]["durability"] == "unknown"
    assert events(new) == [] and next(iter(new.subscribers)).empty()


@pytest.mark.asyncio
async def test_final_drop_only_pending_entries_not_drained_or_retry(journal):
    run = journal()
    worker = live(agent_runs._EffectRecorder(run))
    first = st.steer_worker_receipt("child", "first")
    st.pending_steers("child")
    second = st.steer_worker_receipt("child", "last round pending")
    worker.accepts_steers = False  # Attempt closes; queue remains for a retry.
    assert worker._steering_receipts[second["receipt_id"]]["state"] == "queued"
    worker._steering_attempt_id = "attempt-b"
    worker.accepts_steers = True
    st.pending_steers("child")
    third = st.steer_worker_receipt("child", "final pending")
    st._drop_pending_steering(worker)
    assert worker.steer_queue == [] and worker._steering_entry_receipts == {}
    assert worker._steering_receipts[first["receipt_id"]]["state"] == "drained"
    assert worker._steering_receipts[second["receipt_id"]]["state"] == "drained"
    assert worker._steering_receipts[third["receipt_id"]]["state"] == "dropped"
    assert events(run)[-1]["state"] == "dropped"


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer_drains", [False, True])
async def test_real_drain_dispatch_captures_parent_before_replacement_for_worker_reviewer(journal, history, monkeypatch, reviewer_drains):
    old, new = journal(), journal()
    seen = []
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda *a: True)
    async def worker(run, **kw):
        seen.append(run)
        run.session_id = run.id
        run.parent_session_id = kw["parent_session_id"]
        history.create_session(session_id=run.id, name=run.name, endpoint_url="http://unused", model="fixture")
        run._steering_attempt_id = "attempt-" + run.role
        run.accepts_steers = True
        st._ACTIVE_WORKERS[run.id] = asyncio.current_task()
        st._WORKER_RUNS[run.id] = run
        assert run._steering_recorder.run_id == old.run_id
        assert st.steer_worker(run.id, "private body")
        if run.role == "reviewer" and reviewer_drains:
            st.pending_steers(run.id)
        run.text, run.stop_reason = "Result", "complete"
        run.mutations = ["synthetic.txt"]
    monkeypatch.setattr(st, "_run_subagent", worker)
    async def stream():
        agent_runs._RUNS["parent"] = new
        block = function_call_to_tool_block("delegate_agents", json.dumps({"tasks": [
            {"name": "worker", "instruction": "Inspect fixture"}], "reviewer": True}))
        _, result = await tool_execution.execute_tool_block(block, session_id="parent", owner="fixture",
            call_id="native-call", security_context=ToolRunSecurityContext())
        assert result["exit_code"] == 0
        yield "data: [DONE]\n\n"
    await agent_runs._drain("parent", old, stream())
    assert len(seen) == 2
    assert [event["state"] for event in events(old)] == ["queued", "dropped", "queued", "drained" if reviewer_drains else "dropped"]
    assert events(new) == [] and next(iter(new.subscribers)).empty()
    assert all(event["parent_run_id"] == old.run_id for event in events(old))


@pytest.mark.asyncio
async def test_real_worker_attempt_retry_preserves_entry_identity_and_attempt_provenance(journal, worker, monkeypatch):
    from src import agent_loop
    parent = journal()
    worker._steering_recorder = agent_runs._EffectRecorder(parent)
    worker.parent_run_id = parent.run_id
    worker.delegation_id = "delegation"
    accepted, attempts = [], []
    async def stream(*a, **kw):
        register(worker)
        attempts.append(worker._steering_attempt_id)
        if len(attempts) == 1:
            accepted.append(st.steer_worker_receipt(worker.session_id, "Retry instruction"))
            yield 'data: {"type":"rounds_exhausted"}\n\n'
        else:
            assert kw["pending_user_messages"]() == [{"text": "Retry instruction", "source": "user"}]
        yield 'data: [DONE]\n\n'
    async def emit(event):
        pass
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert len(worker.steer_queue) == 1
    assert [event["state"] for event in events(parent)] == ["queued"]
    await attempt(worker, emit)
    assert attempts[0] != attempts[1]
    observed = events(parent)
    assert observed[0]["accepted_attempt_run_id"] == attempts[0]
    assert observed[1]["receipt_id"] == accepted[0]["receipt_id"]
    assert observed[1]["accepted_attempt_run_id"] == attempts[0]
    assert observed[1]["drained_attempt_run_id"] == attempts[1]
    assert "applied" not in {event["state"] for event in observed}


@pytest.mark.asyncio
async def test_closed_gate_rejection_creates_no_receipt(journal):
    run = journal()
    worker = live(agent_runs._EffectRecorder(run))
    worker.accepts_steers = False
    assert st.steer_worker_receipt("child", "late") is None
    assert st.steer_worker("child", "late") is False
    assert events(run) == [] and worker.steer_queue == []


@pytest.mark.asyncio
async def test_live_history_bounded_without_losing_pending_entry_ids(journal):
    worker = live()
    receipts = [st.steer_worker_receipt("child", "same") for _ in range(1025)]
    assert len(worker._steering_receipts) == 1024
    assert len(worker._steering_entry_receipts) == 1025
    first = worker._steering_entry_receipts[id(worker.steer_queue[0])]
    assert first["receipt_id"] == receipts[0]["receipt_id"]
    assert len(st.pending_steers("child")) == 1025
    assert first["state"] == "drained"
    assert worker._steering_entry_receipts == {}


@pytest.mark.asyncio
async def test_queued_observation_occurs_after_entry_is_linked(journal):
    worker = live()
    class Observer:
        def record_steering(self, **event):
            assert len(worker.steer_queue) == 1
            assert worker._steering_entry_receipts[id(worker.steer_queue[0])]["receipt_id"] == event["receipt_id"]
    worker._steering_recorder = Observer()
    assert st.steer_worker_receipt("child", "synthetic")["durability"] == "durable"


@pytest.mark.asyncio
async def test_missing_real_attempt_never_claims_durable_receipt(journal):
    run = journal()
    worker = live(agent_runs._EffectRecorder(run))
    worker._steering_attempt_id = None
    receipt = st.steer_worker_receipt("child", "legacy live fixture")
    assert receipt["state"] == "queued" and receipt["durability"] == "unknown"
    assert receipt["accepted_attempt_run_id"] == ""
    assert events(run) == []
