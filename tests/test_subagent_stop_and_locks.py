"""BUG 4 and BUG 5 — src/agent_tools/subagent_tools.py.

BUG 4: `one()` swallowed `asyncio.CancelledError` (it was written for
`stop_worker`, which cancels ONE worker while the coordinator carries on).
The sequential branch runs for ANY single-task delegation — the condition is
`if args["parallel"] and len(runs) > 1` — so when the user pressed Stop the
coordinator's cancellation was absorbed by the current worker and the loop
calmly started the NEXT worker, and then the reviewer. Stop did not stop.

BUG 5a: `_targets()` flattened `tool_capabilities._write_targets`'s "cannot be
determined" `None` (fail CLOSED there) into `[]`, which here means "don't
block" (fail OPEN). Worker B could `apply_patch`-delete a file worker A owns.

BUG 5b: locks were keyed by `run.name`, which the MODEL writes. Two tasks with
the same name were the same owner, so they never blocked each other, and
`_build_report_text`'s "changed by more than one worker" warning deduplicated
them away too. `run.id` (`sa{i}-{hex}`) is the identity; `run.name` stays the
label.
"""

import asyncio
import json

import pytest

from src.agent_tools import subagent_tools as st


# ── shared harness (mirrors tests/test_subagents_v2.py) ────────────────────

class _SM:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = type(
            "S", (), {"messages": [], "add_message": lambda self, m: self.messages.append(m)})()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


def _harness_summary(mutations):
    return "data: " + json.dumps({"type": "harness_summary",
                                  "data": {"mutations": mutations, "stop_reason": "complete"}}) + "\n\n"


@pytest.fixture
def delegation(tmp_path, monkeypatch):
    """Wire DelegateAgentsTool to a fake model route; returns a runner."""
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te

    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:11434/v1", "model": "m",
                            "headers": None, "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)

    def _install(loop_fn):
        monkeypatch.setattr(al, "stream_agent_loop", loop_fn)
    return _install


# ── BUG 4: Stop must stop ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stopping_the_coordinator_does_not_launch_the_next_worker(delegation):
    """3 sequential tasks + a reviewer. Cancel the COORDINATOR while worker 1
    is running: workers 2 and 3 and the reviewer must never start."""
    started = []
    worker1_running = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        text = messages[0]["content"]
        name = text.rsplit("YOUR TASK: ", 1)[-1][:20]
        started.append("reviewer" if "REVIEWER sub-agent" in text else name)
        if name.startswith("one"):
            worker1_running.set()
            await asyncio.sleep(30)          # cancelled long before this returns
        yield _harness_summary(["src/x.py"])
        yield "data: [DONE]\n\n"
    delegation(_loop)

    tool = st.DelegateAgentsTool()
    coordinator = asyncio.create_task(tool.execute(json.dumps({
        "tasks": [{"name": "w1", "instruction": "one"},
                  {"name": "w2", "instruction": "two"},
                  {"name": "w3", "instruction": "three"}],
        "parallel": False, "reviewer": True, "timeout_s": 60,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}))

    await asyncio.wait_for(worker1_running.wait(), 5)
    coordinator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(coordinator, 5)

    await asyncio.sleep(0.2)     # give a wrongly-resumed loop time to show up
    assert started == ["one"], f"Stop did not stop the delegation: {started}"


@pytest.mark.asyncio
async def test_a_single_task_delegation_also_honours_stop(delegation):
    """`parallel` is irrelevant for one task: the condition is
    `parallel and len(runs) > 1`, so a lone task always takes this branch."""
    running = asyncio.Event()
    finished = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        running.set()
        await asyncio.sleep(30)
        finished.append(1)
        yield "data: [DONE]\n\n"
    delegation(_loop)

    tool = st.DelegateAgentsTool()
    coordinator = asyncio.create_task(tool.execute(json.dumps({
        "tasks": [{"name": "only", "instruction": "the one task"}],
        "parallel": True, "timeout_s": 60,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}))
    await asyncio.wait_for(running.wait(), 5)
    coordinator.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(coordinator, 5)
    assert finished == []


@pytest.mark.asyncio
async def test_stop_worker_still_only_stops_that_worker(delegation):
    """The flag must distinguish the two cancellations: stop_worker keeps the
    old behaviour (the coordinator finishes the remaining tasks)."""
    slow_running = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        text = messages[0]["content"]
        if "slow task" in text:
            slow_running.set()
            await asyncio.sleep(30)
        yield _harness_summary(["src/b.py"])
        yield "data: [DONE]\n\n"
    delegation(_loop)

    tool = st.DelegateAgentsTool()
    task = asyncio.create_task(tool.execute(json.dumps({
        "tasks": [{"name": "slow", "instruction": "slow task"},
                  {"name": "fast", "instruction": "fast task"}],
        "parallel": False, "timeout_s": 60,
    }), {"session_id": "parent", "owner": None, "progress_cb": None}))
    await asyncio.wait_for(slow_running.wait(), 5)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if st.active_worker_ids():
            break
    assert st.stop_worker(st.active_worker_ids()[0]) is True

    result = await asyncio.wait_for(task, 5)
    reps = {r["name"]: r for r in result["subagents"]}
    assert reps["slow"]["stop_reason"] == "stopped"
    assert reps["fast"]["stop_reason"] == "complete", "stop_worker must not abort the delegation"


# ── BUG 5a: an undeterminable write target fails CLOSED ────────────────────

_DELETE_PATCH = "*** Begin Patch\n*** Delete File: src/a.py\n*** End Patch\n"


def test_targets_distinguishes_undeterminable_from_empty():
    assert st._targets("apply_patch", _DELETE_PATCH) is None
    assert st._targets("write_file", json.dumps({"path": "src/a.py"})) == ["src/a.py"]
    assert st._targets("write_file", json.dumps({"no": "path"})) is None


def _as_worker(reg, worker, fn):
    async def _run():
        st._LOCK_CTX.set(st._LockGuard(reg, worker))
        try:
            return fn()
        finally:
            st._LOCK_CTX.set(None)
    return asyncio.run(_run())


def test_delete_patch_cannot_bypass_another_workers_lock(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    reg.claim("A", ["src/a.py"])
    reason = _as_worker(reg, "B", lambda: st.write_block_reason("apply_patch", _DELETE_PATCH))
    assert reason, "a deleting apply_patch bypassed the sub-agent file lock"
    assert "src/a.py" in reason
    assert reg.conflicts and reg.conflicts[0]["worker"] == "B"


def test_undeterminable_target_is_allowed_when_nobody_else_owns_anything(tmp_path):
    """Fail closed only when there is something to protect: a lone worker (or
    the first one to move) must not be blocked by its own reservations."""
    reg = st.FileLockRegistry(str(tmp_path))
    assert _as_worker(reg, "A", lambda: st.write_block_reason("apply_patch", _DELETE_PATCH)) is None
    reg.claim("A", ["src/a.py"])
    assert _as_worker(reg, "A", lambda: st.write_block_reason("apply_patch", _DELETE_PATCH)) is None


def test_note_write_result_survives_an_undeterminable_target(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    _as_worker(reg, "A", lambda: st.note_write_result("apply_patch", _DELETE_PATCH,
                                                      {"output": "ok", "exit_code": 0}))
    assert reg.owner == {}


# ── BUG 5b: locks are keyed by run.id, not by the model-chosen name ────────

@pytest.mark.asyncio
async def test_two_workers_with_the_same_name_are_distinct_owners_and_a_finished_worker_frees_its_files(delegation):
    """Locks are keyed by run.id, never by the model-written name (two
    "worker"s used to be one owner). And a lock lives as long as its worker:
    once the first has FINISHED, the second — the dependent task in a
    sequential run — may edit the same file (it used to be refused)."""
    reasons = []
    owners = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        guard = st._LOCK_CTX.get()
        owners.append((guard.worker, guard.registry.owner.get(guard.registry.norm("src/shared.py"))))
        reasons.append(st.write_block_reason("edit_file", json.dumps({"path": "src/shared.py"})))
        yield _harness_summary(["src/shared.py"])
        yield "data: [DONE]\n\n"
    delegation(_loop)

    tool = st.DelegateAgentsTool()
    result = await tool.execute(json.dumps({
        "tasks": [{"name": "worker", "instruction": "first", "files": ["src/shared.py"]},
                  {"name": "worker", "instruction": "second"}],
        "parallel": False, "timeout_s": 60,
    }), {"session_id": "parent", "owner": None, "progress_cb": None})

    assert reasons[0] is None, "the owner of a file must not be blocked from it"
    assert reasons[1] is None, "the first worker had finished: its files are free for the dependent task"
    (k1, o1), (k2, o2) = owners
    assert k1 != k2 and o1 == k1 and o2 is None          # distinct keys; released before the second ran
    # while the first still holds the file, a same-named second worker IS blocked
    reg = st.FileLockRegistry(None)
    reg.names["sa0-a"] = reg.names["sa1-b"] = "worker"
    reg.claim("sa0-a", ["src/shared.py"])
    assert reg.blocked_by("sa1-b", ["src/shared.py"]) == "sa0-a"
    assert reg.release("sa0-a") == ["src/shared.py"] and reg.blocked_by("sa1-b", ["src/shared.py"]) is None
    # The two runs are distinct owners, so the overlap warning survives too.
    assert "WARNING — files changed by MORE THAN ONE worker" in result["output"]


def test_report_overlap_warning_is_per_run_not_per_name():
    r1 = st.SubagentRun(0, {"name": "worker", "instruction": "a"})
    r2 = st.SubagentRun(1, {"name": "worker", "instruction": "b"})
    r1.mutations = ["src/shared.py"]
    r2.mutations = ["src/shared.py"]
    assert r1.id != r2.id
    text = st._build_report_text([r1, r2], None, st.FileLockRegistry(None))
    assert "WARNING — files changed by MORE THAN ONE worker" in text
    assert "src/shared.py: worker, worker" in text


def test_a_single_run_touching_a_file_twice_is_not_an_overlap():
    r = st.SubagentRun(0, {"name": "solo", "instruction": "a"})
    r.mutations = ["src/x.py", "src/x.py"]
    assert "MORE THAN ONE worker" not in st._build_report_text([r], None, st.FileLockRegistry(None))


def test_the_reviewer_is_still_excluded_from_the_overlap_warning():
    w = st.SubagentRun(0, {"name": "w", "instruction": "a"})
    rev = st.SubagentRun(1, {"name": st.REVIEWER_NAME, "instruction": "r"}, role="reviewer")
    w.mutations = rev.mutations = ["src/x.py"]
    assert "MORE THAN ONE worker" not in st._build_report_text([w, rev], None, st.FileLockRegistry(None))
