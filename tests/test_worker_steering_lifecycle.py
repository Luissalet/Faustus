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


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["rounds_exhausted", "budget_exceeded", "intent_nudge_exhausted", "budget_exhausted", "cancelled"])
async def test_terminal_guard_rejects_late_input_preserves_queue_and_retry(worker, monkeypatch, terminal):
    attempts = []
    observed_guards = []
    async def stream(*a, **kw):
        register(worker)
        attempts.append(True)
        if len(attempts) == 1:
            assert st.steer_worker(worker.session_id, "Previously accepted")
            yield "data: " + json.dumps({"type": terminal}) + "\n\n"
        else:
            pending = kw["pending_user_messages"]()
            assert pending == [{"text": "Previously accepted", "source": "user"}]
            injected, *_ = agent_loop._apply_steers_to_messages([], pending)
            for ev in injected:
                yield "data: " + json.dumps(ev) + "\n\n"
        yield "data: [DONE]\n\n"
    async def emit(event):
        if event["event"] == "guard":
            observed_guards.append(event["kind"])
            assert not asyncio.current_task().done()
            assert not worker.accepts_steers
            assert not st.steer_worker(worker.session_id, "Late terminal input")
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert observed_guards == [terminal]
    assert worker.steered == 0 and len(worker.steer_queue) == 1
    await attempt(worker, emit)
    assert worker.steered == 1 and worker.steer_queue == []


@pytest.mark.asyncio
async def test_loop_breaker_recovery_stays_open_and_injects(worker, monkeypatch):
    async def stream(*a, **kw):
        register(worker)
        yield 'data: {"type":"loop_breaker_triggered"}\n\n'
        pending = kw["pending_user_messages"]()
        messages = []
        injected, *_ = agent_loop._apply_steers_to_messages(messages, pending)
        assert messages[-1] == {"role": "user", "content": "Recovery input"}
        for event in injected:
            yield "data: " + json.dumps(event) + "\n\n"
    async def emit(event):
        if event["event"] == "guard":
            assert worker.accepts_steers
            assert st.steer_worker(worker.session_id, "Recovery input")
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)
    assert worker.steered == 1 and worker.steer_queue == []


@pytest.mark.parametrize("terminal", ["rounds_exhausted", "budget_exceeded", "intent_nudge_exhausted"])
def test_real_guard_callsites_end_rounds_instead_of_recovering(terminal):
    import ast
    from pathlib import Path
    tree = ast.parse(Path(agent_loop.__file__).read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    event = next(node for node in ast.walk(tree) if isinstance(node, ast.Dict)
        and any(isinstance(key, ast.Constant) and key.value == "type"
            and isinstance(value, ast.Constant) and value.value == terminal
            for key, value in zip(node.keys, node.values)))
    ancestors = []
    while event in parents:
        event = parents[event]
        ancestors.append(event)
    guard = next(node for node in ancestors if isinstance(node, ast.If))
    if terminal == "rounds_exhausted":
        assert not any(isinstance(node, (ast.For, ast.AsyncFor, ast.While)) for node in ancestors)
    else:
        assert isinstance(guard.body[-1], ast.Break)
        if terminal == "budget_exceeded":
            assert any(isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == "budget_hit" for target in node.targets)
                and isinstance(node.value, ast.Constant) and node.value.value is True for node in guard.body)
            budget_exit = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                and isinstance(node.test, ast.Name) and node.test.id == "budget_hit")
            assert isinstance(budget_exit.body[-1], ast.Break)


@pytest.mark.asyncio
async def test_nonterminal_budget_metrics_leave_admission_open(worker, monkeypatch):
    async def stream(*a, **kw):
        register(worker)
        yield 'data: {"type":"metrics","data":{"budget":{"used":1,"limit":10}}}\n\n'
        assert worker.accepts_steers
        assert st.steer_worker(worker.session_id, "Budget remains available")
        assert kw["pending_user_messages"]()[0]["text"] == "Budget remains available"
    async def emit(event):
        pass
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    await attempt(worker, emit)


def test_all_real_budget_exhausted_callsites_stop_instead_of_recovering():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(agent_loop.__file__).read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Yield)
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_budget_exhausted_event"]
    assert len(calls) == 4
    guards = []
    for node in calls:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If):
                guards.append(node)
                break
    assert all(isinstance(guard.body[-1], ast.Break) for guard in guards)
    flags = []
    for guard in guards:
        loop = guard
        while loop in parents and not isinstance(loop, (ast.For, ast.AsyncFor, ast.While)):
            loop = parents[loop]
        if isinstance(loop, ast.For):
            assert any(isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                and target.id == "budget_hit" for target in node.targets)
                and isinstance(node.value, ast.Constant) and node.value.value is True for node in guard.body)
            flags.append("budget_hit")
        elif isinstance(loop, ast.AsyncFor):
            assert isinstance(guard.test, ast.Compare)
            assert isinstance(guard.test.left, ast.Name)
            assert guard.test.left.id == "_compaction_budget_exhaustion"
            flags.append("_compaction_budget_exhaustion")
    assert sorted(flags) == ["_compaction_budget_exhaustion", "budget_hit", "budget_hit"]
    for flag in set(flags):
        exits = [node for node in ast.walk(tree) if isinstance(node, ast.If)
            and any(isinstance(name, ast.Name) and name.id == flag for name in ast.walk(node.test))
            and isinstance(node.body[-1], ast.Break)]
        assert any(isinstance(parents[node], ast.While) for node in exits)



def test_all_cancelled_callsites_exit_rounds_with_recovery_latch():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(agent_loop.__file__).read_text(encoding="utf-8"))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    events = [node for node in ast.walk(tree) if isinstance(node, ast.Dict)
        and any(isinstance(key, ast.Constant) and key.value == "type"
            and isinstance(value, ast.Constant) and value.value == "cancelled"
            for key, value in zip(node.keys, node.values))]
    assert len(events) == 4
    flags = set()
    for event in events:
        node = event
        guards = []
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If):
                guards.append(node)
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                loop = node
                break
        guard = guards[0]
        assert isinstance(guard.body[-1], ast.Break)
        if isinstance(loop, ast.While):
            continue
        flag = "_cancel_hit" if isinstance(loop, ast.For) else "_recovery_cancelled"
        assignments = [node for node in guard.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == flag for target in node.targets)
            and isinstance(node.value, ast.Constant) and node.value.value is True]
        assert len(assignments) == 1
        if flag == "_recovery_cancelled":
            assert assignments[0].lineno < event.lineno  # Latched before fanout.
        flags.add(flag)
    assert flags == {"_cancel_hit", "_recovery_cancelled"}
    for flag in flags:
        exits = [node for node in ast.walk(tree) if isinstance(node, ast.If)
            and any(isinstance(name, ast.Name) and name.id == flag for name in ast.walk(node.test))
            and isinstance(node.body[-1], ast.Break)]
        assert any(isinstance(parents[node], ast.While) for node in exits)
