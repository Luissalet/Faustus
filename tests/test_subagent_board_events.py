"""Live control board for delegate_agents workers — the backend half.

Event contract (every `{"subagent": {...}}` payload keeps id/index/name/role/
event and ADDS `ts` + `session_id`):

* `started`   + started_at, instruction, files, model, role, max_rounds, timeout_s
* `round`     forwarded from the worker's `round_info`
* `tool`/progress  forwarded from the worker's own `tool_progress` (bash tail)
* `tick`      watchdog heartbeat: elapsed_s, idle_s, round, last_tool,
              tool_calls, input_tokens, output_tokens, stalled, stall_reason
* `queued`    while waiting for a slot (agent_subagent_max_parallel)
* `steer`     when a steering message was injected (source user|supervisor)
* `supervisor` nudge | stop (deterministic control agent, no LLM calls)
* `done`      + input_tokens, output_tokens, rounds, started_at, ended_at,
              role, files, model, instruction, steered

Plus the bugs the audit reproduced: bool("false") (B4), the check-then-act
file lock (B5), the transcript saved before the stop reason is known (B6),
the silently dropped fifth task, and stop_for_session ignoring workers (B1).
"""

import asyncio
import json
import time

import pytest

from src import agent_runs
from src.agent_tools import subagent_tools as st


# ── shared harness (mirrors tests/test_subagent_stop_and_locks.py) ─────────

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


def _ev(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _harness_summary(mutations, stop_reason="complete"):
    return _ev({"type": "harness_summary", "data": {"mutations": mutations, "stop_reason": stop_reason}})


@pytest.fixture
def delegation(tmp_path, monkeypatch):
    """Wire DelegateAgentsTool to a fake model route; returns (install, sm)."""
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
    # Fast, deterministic watchdog defaults; tests override what they need.
    knobs = {"agent_subagent_tick_seconds": 0.05, "agent_subagent_stall_seconds": 100,
             "agent_subagent_supervisor": True, "agent_subagent_max_parallel": 4}
    monkeypatch.setattr(st, "_setting", lambda k, d=None: knobs.get(k, d))

    def _install(loop_fn):
        monkeypatch.setattr(al, "stream_agent_loop", loop_fn)
    _install.knobs = knobs
    _install.sm = sm
    return _install


async def _delegate(tasks, events, **extra):
    async def _cb(payload):
        events.append(payload["subagent"])
    tool = st.DelegateAgentsTool()
    body = {"tasks": tasks, "timeout_s": 60}
    body.update(extra)
    return await tool.execute(json.dumps(body), {"session_id": "parent", "owner": None, "progress_cb": _cb})


# ── B4: "false" is not True ────────────────────────────────────────────────

def test_parse_accepts_string_booleans():
    args = st.parse_delegation_args(json.dumps({"tasks": ["a"], "parallel": "false", "reviewer": "false"}))
    assert args["parallel"] is False and args["reviewer"] is False
    args = st.parse_delegation_args(json.dumps({"tasks": ["a"], "parallel": "no", "reviewer": "yes"}))
    assert args["parallel"] is False and args["reviewer"] is True
    args = st.parse_delegation_args(json.dumps({"tasks": ["a"], "parallel": 0, "reviewer": 1}))
    assert args["parallel"] is False and args["reviewer"] is True


# ── the fifth task is not dropped silently ─────────────────────────────────

def test_parse_reports_dropped_tasks():
    args = st.parse_delegation_args(json.dumps({"tasks": [f"task {i}" for i in range(6)]}))
    assert len(args["tasks"]) == st.MAX_SUBAGENTS
    assert args["dropped_tasks"] == 2
    assert st.parse_delegation_args(json.dumps({"tasks": ["a"]}))["dropped_tasks"] == 0


@pytest.mark.asyncio
async def test_tool_result_tells_the_model_about_dropped_tasks(delegation):
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _harness_summary([])
        yield "data: [DONE]\n\n"
    delegation(_loop)
    result = await _delegate([f"task {i}" for i in range(6)], [], parallel=False)
    assert result["dropped_tasks"] == 2
    assert "2 task(s) were NOT run" in result["output"] and "delegate_agents again" in result["output"]


# ── B5: the lock claims at check time, not after the write ────────────────

def _as_worker(reg, worker, fn):
    async def _run():
        st._LOCK_CTX.set(st._LockGuard(reg, worker))
        try:
            return fn()
        finally:
            st._LOCK_CTX.set(None)
    return asyncio.run(_run())


def test_write_check_claims_an_unowned_path_so_a_parallel_writer_is_blocked(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    call = json.dumps({"path": "src/shared.py", "content": "x"})
    assert _as_worker(reg, "A", lambda: st.write_block_reason("write_file", call)) is None
    # A's write is still in flight — B checks the same file NOW.
    reason = _as_worker(reg, "B", lambda: st.write_block_reason("write_file", call))
    assert reason and "src/shared.py" in reason, "check-then-act: both workers were let through"
    assert reg.owner[reg.norm("src/shared.py")] == "A"


def test_a_failed_write_releases_the_provisional_claim(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    call = json.dumps({"path": "src/new.py", "content": "x"})
    _as_worker(reg, "A", lambda: st.write_block_reason("write_file", call))
    _as_worker(reg, "A", lambda: st.note_write_result("write_file", call, {"error": "disk full", "exit_code": 1}))
    assert reg.norm("src/new.py") not in reg.owner, "a failed write must not keep the file"
    # B may now take it.
    assert _as_worker(reg, "B", lambda: st.write_block_reason("write_file", call)) is None
    assert reg.owner[reg.norm("src/new.py")] == "B"


def test_a_failed_write_never_releases_a_declared_file(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    reg.claim("A", ["src/mine.py"])
    call = json.dumps({"path": "src/mine.py", "content": "x"})
    _as_worker(reg, "A", lambda: st.write_block_reason("write_file", call))
    _as_worker(reg, "A", lambda: st.note_write_result("write_file", call, {"error": "nope", "exit_code": 1}))
    assert reg.owner[reg.norm("src/mine.py")] == "A"


# ── every payload: ts + session_id; started carries the task card ─────────

@pytest.mark.asyncio
async def test_every_event_carries_ts_and_session_id_and_started_has_the_card(delegation):
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "tool_start", "tool": "bash", "command": "ls"})
        yield _ev({"type": "tool_output", "tool": "bash", "command": "ls", "output": "a", "exit_code": 0})
        yield _harness_summary([])
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    t0 = time.time()
    await _delegate([{"name": "w", "instruction": "do it", "files": ["a.py"], "model": "qwen:x"}], events,
                    max_rounds=7, timeout_s=90)
    assert events, "no events"
    for e in events:
        assert isinstance(e.get("ts"), float) and e["ts"] >= t0 - 1, e
        assert "session_id" in e, e
        for k in ("id", "index", "name", "role", "event", "delegation"):
            assert k in e, (k, e)
    # One `delegation` id per delegate_agents call, identical on every event —
    # the board keys its state by it (a second /agents in the same chat must
    # not pile onto the first one's N/M count).
    assert len({e["delegation"] for e in events}) == 1 and len(events[0]["delegation"]) == 8
    started = next(e for e in events if e["event"] == "started")
    assert started["session_id"] and isinstance(started["started_at"], float)
    assert started["files"] == ["a.py"] and started["model"] == "qwen:x" and started["role"] == "worker"
    assert started["max_rounds"] == 7 and started["timeout_s"] == 90 and started["instruction"] == "do it"
    assert all(e["session_id"] == started["session_id"] for e in events if e["event"] != "queued")


# ── round + progress forwarded, tokens accumulated, done is complete ──────

@pytest.mark.asyncio
async def test_round_progress_and_token_events_are_forwarded(delegation):
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "round_info", "round": 1, "input_tokens": 100, "output_tokens": 20})
        yield _ev({"type": "tool_start", "tool": "bash", "command": "npm test"})
        yield _ev({"type": "tool_progress", "tool": "bash", "round": 1, "elapsed_s": 2.0, "tail": "x" * 500})
        yield _ev({"type": "tool_progress", "tool": "bash", "round": 1, "elapsed_s": 4.0, "tail": "done"})
        yield _ev({"type": "tool_output", "tool": "bash", "command": "npm test", "output": "ok", "exit_code": 0})
        yield _ev({"type": "round_info", "round": 2, "input_tokens": 150, "output_tokens": 30})
        yield _harness_summary(["src/a.py"])
        yield _ev({"type": "metrics", "data": {"input_tokens": 250, "output_tokens": 50}})
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    result = await _delegate([{"name": "w", "instruction": "run tests"}], events)
    rounds = [e for e in events if e["event"] == "round"]
    assert [r["round"] for r in rounds] == [1, 2]
    prog = [e for e in events if e["event"] == "tool" and e.get("phase") == "progress"]
    assert len(prog) == 2 and prog[0]["tool"] == "bash" and prog[0]["elapsed_s"] == 2.0
    assert len(prog[0]["tail"]) <= 200 and prog[1]["tail"] == "done"
    done = next(e for e in events if e["event"] == "done")
    assert done["input_tokens"] == 250 and done["output_tokens"] == 50 and done["rounds"] == 2
    assert done["started_at"] and done["ended_at"] >= done["started_at"]
    assert done["role"] == "worker" and done["files"] == [] and done["instruction"] == "run tests"
    assert done["steered"] == 0 and done["supervisor"] == []
    rep = result["subagents"][0]
    for k in ("input_tokens", "output_tokens", "started_at", "ended_at", "role", "files", "model",
              "instruction", "rounds", "steered", "supervisor"):
        assert k in rep, k


@pytest.mark.asyncio
async def test_tokens_accumulate_from_rounds_when_metrics_never_arrive(delegation):
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "round_info", "round": 1, "input_tokens": 100, "output_tokens": 20})
        yield _ev({"type": "round_info", "round": 2, "input_tokens": 150, "output_tokens": 30})
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    await _delegate([{"name": "w", "instruction": "x"}], events)
    done = next(e for e in events if e["event"] == "done")
    assert done["input_tokens"] == 250 and done["output_tokens"] == 50


# ── tick: the watchdog heartbeat ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_reports_elapsed_idle_round_tool_and_tokens(delegation):
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "round_info", "round": 1, "input_tokens": 10, "output_tokens": 5})
        yield _ev({"type": "tool_start", "tool": "bash", "command": "sleep 1"})
        await asyncio.sleep(0.4)
        yield _ev({"type": "tool_output", "tool": "bash", "command": "sleep 1", "output": "", "exit_code": 0})
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    await _delegate([{"name": "w", "instruction": "x"}], events)
    ticks = [e for e in events if e["event"] == "tick"]
    assert len(ticks) >= 2, "the watchdog never ticked"
    t = ticks[-1]
    assert t["elapsed_s"] >= 0.2 and t["idle_s"] >= 0.1 and t["idle_s"] <= t["elapsed_s"] + 0.01
    assert t["round"] == 1 and t["last_tool"] == "bash" and t["tool_calls"] == 0
    assert t["input_tokens"] == 10 and t["output_tokens"] == 5
    assert t["stalled"] is False and t["stall_reason"] is None
    # ticks stop with the worker: none after done
    idx_done = max(i for i, e in enumerate(events) if e["event"] == "done")
    assert not [e for e in events[idx_done + 1:] if e["event"] == "tick"]


@pytest.mark.asyncio
async def test_supervisor_nudges_then_stops_a_stalled_worker(delegation):
    delegation.knobs["agent_subagent_stall_seconds"] = 0.2
    seen_steers = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        cb = kwargs.get("pending_user_messages")
        yield _ev({"type": "tool_start", "tool": "bash", "command": "sleep 999"})
        try:
            while True:                      # silent forever: only a cancel ends it
                await asyncio.sleep(0.05)
                if cb is not None:
                    seen_steers.extend(cb() or [])
        finally:
            pass
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    result = await asyncio.wait_for(_delegate([{"name": "w", "instruction": "x"}], events), 10)
    sup = [e for e in events if e["event"] == "supervisor"]
    assert [s["action"] for s in sup] == ["nudge", "stop"], sup
    assert "stuck" in sup[0]["reason"].lower() or "idle" in sup[0]["reason"].lower()
    stalled_ticks = [e for e in events if e["event"] == "tick" and e["stalled"]]
    assert stalled_ticks and stalled_ticks[0]["stall_reason"]
    assert seen_steers and seen_steers[0]["source"] == "supervisor" and "stuck" in seen_steers[0]["text"]
    done = next(e for e in events if e["event"] == "done")
    assert done["stop_reason"] == "stalled" and done["error"] is None
    assert [a["action"] for a in done["supervisor"]] == ["nudge", "stop"]
    rep = result["subagents"][0]
    assert rep["stop_reason"] == "stalled"
    assert "supervisor" in result["output"].lower() and "stalled" in result["output"].lower()
    assert not st.active_worker_ids()


@pytest.mark.asyncio
async def test_supervisor_can_be_disabled(delegation):
    delegation.knobs["agent_subagent_stall_seconds"] = 0.1
    delegation.knobs["agent_subagent_supervisor"] = False
    gate = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        await gate.wait()
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    task = asyncio.create_task(_delegate([{"name": "w", "instruction": "x"}], events))
    await asyncio.sleep(0.5)
    assert any(e["event"] == "tick" and e["stalled"] for e in events)
    assert not [e for e in events if e["event"] == "supervisor"]
    gate.set()
    await asyncio.wait_for(task, 5)
    assert next(e for e in events if e["event"] == "done")["stop_reason"] == "complete"


@pytest.mark.asyncio
async def test_loop_detection_marks_the_tick_stalled(delegation):
    async def _loop(endpoint_url, model, messages, **kwargs):
        for _ in range(3):
            yield _ev({"type": "tool_start", "tool": "bash", "command": "pytest -q", "full_command": "pytest -q"})
            yield _ev({"type": "tool_output", "tool": "bash", "command": "pytest -q", "output": "F", "exit_code": 1})
        await asyncio.sleep(0.2)
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    await _delegate([{"name": "w", "instruction": "x"}], events)
    ticks = [e for e in events if e["event"] == "tick"]
    assert ticks and ticks[-1]["stalled"] is True and ticks[-1]["stall_reason"] == "loop"
    sup = [e for e in events if e["event"] == "supervisor"]
    assert sup and sup[0]["action"] == "nudge" and "loop" in sup[0]["reason"]


# ── steer events ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_steer_is_queued_injected_and_reported(delegation):
    running = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        cb = kwargs["pending_user_messages"]
        running.set()
        for _ in range(60):
            await asyncio.sleep(0.02)
            pending = cb() or []
            for p in pending:
                # the real loop appends a user message and yields this:
                yield _ev({"type": "steer", "round": 2, "text": p["text"], "source": p["source"]})
            if pending:
                break
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    task = asyncio.create_task(_delegate([{"name": "w", "instruction": "x"}], events))
    await asyncio.wait_for(running.wait(), 5)
    for _ in range(100):
        if st.active_worker_ids():
            break
        await asyncio.sleep(0.02)
    sid = st.active_worker_ids()[0]
    assert st.steer_worker(sid, "  focus on tests/  ") is True
    assert st.steer_worker("nope", "x") is False
    result = await asyncio.wait_for(task, 5)
    steer = [e for e in events if e["event"] == "steer"]
    assert steer == [dict(steer[0])] and steer[0]["text"] == "focus on tests/" and steer[0]["source"] == "user"
    done = next(e for e in events if e["event"] == "done")
    assert done["steered"] == 1 and result["subagents"][0]["steered"] == 1
    assert st.steer_worker(sid, "late") is False


# ── B6: transcript metadata knows how the worker ended ────────────────────

@pytest.mark.asyncio
async def test_child_transcript_marks_a_stopped_worker(delegation):
    gate = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        await gate.wait()
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    task = asyncio.create_task(_delegate([{"name": "w", "instruction": "x"}], events))
    for _ in range(100):
        if st.active_worker_ids():
            break
        await asyncio.sleep(0.02)
    sid = st.active_worker_ids()[0]
    assert st.stop_worker(sid)
    await asyncio.wait_for(task, 5)
    child = delegation.sm.get_session(sid)
    assistant = [m for m in child.messages if m.role == "assistant"]
    assert assistant, "transcript not saved"
    meta = assistant[-1].metadata
    assert meta.get("stopped") is True
    assert meta["subagent"]["stop_reason"] == "stopped" and meta["subagent"]["parent_session"] == "parent"


@pytest.mark.asyncio
async def test_child_transcript_marks_a_timed_out_worker(delegation, monkeypatch):
    monkeypatch.setattr(st, "MIN_WORKER_TIMEOUT_S", 0.05)

    async def _loop(endpoint_url, model, messages, **kwargs):
        await asyncio.sleep(30)
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    await asyncio.wait_for(_delegate([{"name": "w", "instruction": "x"}], events, timeout_s=0.2), 5)
    done = next(e for e in events if e["event"] == "done")
    assert done["stop_reason"] == "timeout"
    child = delegation.sm.get_session(done["session_id"])
    meta = [m for m in child.messages if m.role == "assistant"][-1].metadata
    assert meta.get("stopped") is True and meta["subagent"]["stop_reason"] == "timeout"
    assert meta["subagent"].get("timeout") is True


# ── one GPU: at most N workers at once, the rest are queued ───────────────

@pytest.mark.asyncio
async def test_max_parallel_queues_workers_and_timeout_ignores_queue_time(delegation, monkeypatch):
    delegation.knobs["agent_subagent_max_parallel"] = 1
    monkeypatch.setattr(st, "MIN_WORKER_TIMEOUT_S", 0.05)
    concurrent = {"now": 0, "max": 0}

    async def _loop(endpoint_url, model, messages, **kwargs):
        concurrent["now"] += 1
        concurrent["max"] = max(concurrent["max"], concurrent["now"])
        try:
            await asyncio.sleep(0.6)
        finally:
            concurrent["now"] -= 1
        yield _harness_summary([])
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    # 0.6 s queued + 0.6 s running > 1.0 s: only a timeout that starts at
    # `started` lets the second worker finish.
    result = await asyncio.wait_for(_delegate([{"name": "a", "instruction": "one"},
                                               {"name": "b", "instruction": "two"}], events,
                                              parallel=True, timeout_s=1.0), 10)
    assert concurrent["max"] == 1
    by_name = {}
    for e in events:
        by_name.setdefault(e["name"], []).append(e)
    assert [e["event"] for e in by_name["b"]][0] == "queued"
    assert by_name["b"][0]["session_id"] is None
    a_done = next(e for e in by_name["a"] if e["event"] == "done")
    b_started = next(e for e in by_name["b"] if e["event"] == "started")
    assert b_started["ts"] >= a_done["ts"]
    reps = {r["name"]: r for r in result["subagents"]}
    assert reps["b"]["stop_reason"] == "complete" and reps["a"]["stop_reason"] == "complete"
    assert not any(e["event"] == "queued" for e in by_name["a"])


# ── the worker registry behind /api/chat/activity ─────────────────────────

@pytest.mark.asyncio
async def test_worker_board_lists_live_workers(delegation):
    gate = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "round_info", "round": 3})
        yield _ev({"type": "tool_start", "tool": "bash", "command": "x"})
        yield _ev({"type": "tool_output", "tool": "bash", "command": "x", "output": "", "exit_code": 0})
        await gate.wait()
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    task = asyncio.create_task(_delegate([{"name": "w", "instruction": "x"}], events))
    for _ in range(100):
        if st.active_worker_ids():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    board = st.worker_board()
    sid = st.active_worker_ids()[0]
    assert set(board) == {sid}
    w = board[sid]
    assert w["parent"] == "parent" and w["name"] == "w" and w["role"] == "worker"
    assert w["round"] == 3 and w["tool_calls"] == 1 and w["stalled"] is False
    assert isinstance(w["started_at"], float) and w["last_event_at"] >= w["started_at"]
    gate.set()
    await asyncio.wait_for(task, 5)
    assert st.worker_board() == {}


# ── B1: deleting the worker's chat stops the worker ───────────────────────

@pytest.mark.asyncio
async def test_stop_for_session_stops_a_subagent_worker(delegation):
    gate = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        if "slow" in messages[0]["content"]:
            await gate.wait()
        yield _harness_summary([])
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    task = asyncio.create_task(_delegate([{"name": "slow", "instruction": "slow task"},
                                          {"name": "fast", "instruction": "fast task"}], events, parallel=False))
    for _ in range(100):
        if st.active_worker_ids():
            break
        await asyncio.sleep(0.02)
    sid = st.active_worker_ids()[0]
    assert agent_runs.stop_for_session(sid, reason="session_deleted") is True
    result = await asyncio.wait_for(task, 5)
    reps = {r["name"]: r for r in result["subagents"]}
    assert reps["slow"]["stop_reason"] == "stopped" and reps["fast"]["stop_reason"] == "complete"
    assert sid not in st.active_worker_ids()


# ── replay-buffer compaction: progress ticks and watchdog ticks only ──────

def test_compact_key_collapses_progress_and_ticks_but_no_other_subagent_event():
    def ev(**d):
        return "data: " + json.dumps(d) + "\n\n"

    def sa(**d):
        return ev(type="tool_progress", tool="delegate_agents", round=1, subagent={"id": "w1", **d})

    run = agent_runs._Run()
    agent_runs._publish(run, sa(event="started"))
    agent_runs._publish(run, sa(event="tool", phase="progress", tool="bash", elapsed_s=2))
    agent_runs._publish(run, sa(event="tool", phase="progress", tool="bash", elapsed_s=4))
    agent_runs._publish(run, sa(event="tool", phase="progress", tool="bash", elapsed_s=6))
    agent_runs._publish(run, ev(type="tool_progress", tool="delegate_agents", round=1,
                                subagent={"id": "w2", "event": "tool", "phase": "progress", "tool": "bash", "elapsed_s": 1}))
    agent_runs._publish(run, sa(event="tool", phase="done", tool="bash", ok=True))
    agent_runs._publish(run, sa(event="tick", elapsed_s=5))
    agent_runs._publish(run, sa(event="tick", elapsed_s=10))
    agent_runs._publish(run, sa(event="round", round=2))
    agent_runs._publish(run, sa(event="round", round=3))
    agent_runs._publish(run, sa(event="tick", elapsed_s=15))
    agent_runs._publish(run, sa(event="done"))
    kinds = []
    for e in run.buffer:
        d = json.loads(e[6:])["subagent"]
        kinds.append((d["id"], d["event"], d.get("phase"), d.get("elapsed_s", d.get("round"))))
    assert kinds == [
        ("w1", "started", None, None),
        ("w1", "tool", "progress", 6),          # three ticks of one tool → the latest
        ("w2", "tool", "progress", 1),          # another worker: its own slot
        ("w1", "tool", "done", None),
        ("w1", "tick", None, 10),               # consecutive ticks → the latest
        ("w1", "round", None, 2),               # state changes are never merged
        ("w1", "round", None, 3),
        ("w1", "tick", None, 15),
        ("w1", "done", None, None),
    ]


def test_compact_key_still_collapses_plain_tool_progress_and_never_mixes_them():
    def ev(**d):
        return "data: " + json.dumps(d) + "\n\n"
    run = agent_runs._Run()
    agent_runs._publish(run, ev(type="tool_progress", tool="bash", round=1, elapsed_s=2))
    agent_runs._publish(run, ev(type="tool_progress", tool="delegate_agents", round=1,
                                subagent={"id": "w1", "event": "tick", "elapsed_s": 5}))
    agent_runs._publish(run, ev(type="tool_progress", tool="bash", round=1, elapsed_s=4))
    agent_runs._publish(run, ev(type="tool_progress", tool="bash", round=1, elapsed_s=6))
    assert len(run.buffer) == 3


# ── persisted reports restore the board after reload ──────────────────────

def test_compact_subagent_reports_keeps_the_board_fields():
    from src.agent_loop import _compact_subagent_reports
    rep = {
        "id": "sa1-abc", "name": "w", "session_id": "c1", "status": "done", "stop_reason": "complete",
        "error": None, "tool_calls": 3, "failed_calls": 0, "mutations": ["a.py"], "duration_s": 4.2,
        "final_text": "x" * 600, "input_tokens": 100, "output_tokens": 20, "started_at": 1.0, "ended_at": 5.2,
        "role": "worker", "files": ["a.py"], "model": "qwen", "instruction": "y" * 700, "rounds": 2,
        "steered": 1, "supervisor": [{"action": "nudge", "reason": "idle", "ts": 3.0}],
    }
    out = _compact_subagent_reports([rep])[0]
    assert out["input_tokens"] == 100 and out["output_tokens"] == 20
    assert out["started_at"] == 1.0 and out["ended_at"] == 5.2
    assert out["role"] == "worker" and out["files"] == ["a.py"] and out["model"] == "qwen"
    assert out["instruction"] == "y" * 500 and out["rounds"] == 2 and out["steered"] == 1
    assert out["supervisor"] == [{"action": "nudge", "reason": "idle", "ts": 3.0}]
    assert len(out["final_text"]) == 400


def test_settings_have_the_board_knobs():
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_subagent_tick_seconds"] == 5
    assert DEFAULT_SETTINGS["agent_subagent_stall_seconds"] == 120
    assert DEFAULT_SETTINGS["agent_subagent_supervisor"] is True
    assert DEFAULT_SETTINGS["agent_subagent_max_parallel"] == 2


def test_workers_get_a_lean_toolset_unless_the_task_asks_for_more(monkeypatch):
    """Measured: 19 tool schemas = 4.7k tokens = 65 % of a worker's first
    round on qwen3.5:9b. A scoped worker never needs web search, memory,
    skills or background jobs — unless its task says so."""
    from src.agent_tools import subagent_tools as st
    monkeypatch.setattr(st, "_setting", lambda k, d=None: {"agent_subagent_lean_tools": True}.get(k, d))
    lean = st.worker_disabled_tools("[cart.py] add currency_format(amount)")
    assert {"web_search", "web_fetch", "manage_skills", "manage_bg_jobs", "manage_memory"} <= lean
    assert {"delegate_agents", "ask_user", "update_plan"} <= lean          # the hard set stays
    for tool in ("read_file", "edit_file", "bash", "python", "apply_patch", "write_file", "todowrite", "grep", "glob", "ls"):
        assert tool not in lean, tool
    web = st.worker_disabled_tools("Busca en internet la documentación de httpx y resume")
    assert "web_search" not in web and "web_fetch" not in web and "delegate_agents" in web
    url = st.worker_disabled_tools("Lee https://example.com/spec y aplica el formato")
    assert "web_fetch" not in url
    # Audited: one keyword used to restore ALL ten lean-denied tools. A task
    # that mentions memory gets manage_memory back — not the web, skills,
    # background jobs, contacts, notes and the rest.
    memoria = st.worker_disabled_tools("[parser.py] arregla la fuga de memoria del parser")
    assert "manage_memory" not in memoria
    for tool in ("web_search", "web_fetch", "manage_skills", "manage_bg_jobs", "manage_contact", "manage_notes", "ui_control"):
        assert tool in memoria, tool
    assert "delegate_agents" in memoria
    # …and a web task gets the web back but nothing else
    for tool in ("manage_memory", "manage_skills", "manage_bg_jobs", "manage_contact", "manage_notes", "manage_tasks", "ui_control"):
        assert tool in web, tool
    assert "manage_skills" not in st.worker_disabled_tools("Create a skill that lints the repo")
    assert "web_search" in st.worker_disabled_tools("Create a skill that lints the repo")
    bg = st.worker_disabled_tools("Lanza los tests en segundo plano y revisa el resultado")
    assert "manage_bg_jobs" not in bg and "web_search" in bg and "manage_memory" in bg
    assert "manage_bg_jobs" not in st.worker_disabled_tools("run the build as a background job")
    notes = st.worker_disabled_tools("Add a note with the release checklist")
    assert "manage_notes" not in notes and "manage_memory" in notes
    contacts = st.worker_disabled_tools("Look up the contact for the vendor and update the phone")
    assert "manage_contact" not in contacts and "web_search" in contacts
    # a URL in the task keeps only the web family
    assert "manage_memory" in url and "manage_skills" in url
    # "remember" restores memory alone
    assert "manage_memory" not in st.worker_disabled_tools("Remember the API base path for later")
    assert "web_fetch" in st.worker_disabled_tools("Remember the API base path for later")
    monkeypatch.setattr(st, "_setting", lambda k, d=None: {"agent_subagent_lean_tools": False}.get(k, d))
    off = st.worker_disabled_tools("[cart.py] add currency_format(amount)")
    assert off == set(st.SUBAGENT_DISABLED_TOOLS)


def test_the_supervisor_nudge_does_not_tell_a_worker_to_ask_user():
    """Workers run detached with ask_user disabled; a nudge that says 'call
    ask_user' sends them to a tool they do not have."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "agent_tools" / "subagent_tools.py").read_text(encoding="utf-8")
    dog = src[src.index("async def watchdog("):src.index("async def one(")]
    assert "ask_user" not in dog.replace("cannot ask_user", "")
    assert "report what" in dog


# ── the worker model (two cards: a model of their own so they overlap) ──────

@pytest.mark.asyncio
async def test_workers_run_on_the_configured_worker_model_and_a_tasks_own_model_still_wins(delegation):
    """Measured on the two-card box: Ollama generates for two DIFFERENT
    models at once, but two requests to the same model queue on its single
    slot. `agent_subagent_worker_model` gives the workers a model of their
    own (pinned to the other card in Local models); empty = the
    coordinator's; a task's explicit `model` still wins."""
    seen = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        seen.append(model)
        yield _harness_summary([])
        yield "data: [DONE]\n\n"
    delegation(_loop)
    events = []
    await _delegate(["a", {"instruction": "b", "model": "special:1b"}], events, parallel=False)
    assert seen == ["m", "special:1b"]
    assert [e["model"] for e in events if e.get("event") == "started"] == ["m", "special:1b"]

    seen.clear()
    events.clear()
    delegation.knobs["agent_subagent_worker_model"] = "qwen3.5:9b"
    await _delegate(["a", {"instruction": "b", "model": "special:1b"}], events, parallel=False)
    assert seen == ["qwen3.5:9b", "special:1b"]
    assert [e["model"] for e in events if e.get("event") == "started"] == ["qwen3.5:9b", "special:1b"]

    seen.clear()
    delegation.knobs["agent_subagent_worker_model"] = "  auto "
    await _delegate(["a"], [], parallel=False)
    assert seen == ["m"]
