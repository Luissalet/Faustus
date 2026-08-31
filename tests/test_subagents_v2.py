"""Sub-agents v2 (src/agent_tools/subagent_tools.py): exclusive files per
worker (a lock, not a warning), the /agents inline prefixes, the optional
reviewer worker, and stopping one worker."""

import asyncio
import json

import pytest

from src.agent_tools import subagent_tools as st


# ── parsing ────────────────────────────────────────────────────────────────

def test_parse_inline_files_and_model_prefixes():
    args = st.parse_delegation_args(json.dumps({
        "tasks": [
            "{qwen3.5:9b} [src/calc.py, src/util.py] fix add()",
            {"name": "tests", "instruction": "add tests", "files": "tests/test_calc.py"},
            {"instruction": "docs"},
        ],
        "reviewer": True,
    }))
    t0, t1, t2 = args["tasks"]
    assert t0["model"] == "qwen3.5:9b" and t0["files"] == ["src/calc.py", "src/util.py"] and t0["instruction"] == "fix add()"
    assert t0["name"] == "fix add()"
    assert t1["files"] == ["tests/test_calc.py"] and t1["name"] == "tests"
    assert t2["files"] == [] and t2["model"] == ""
    assert args["reviewer"] is True


def test_reviewer_defaults_to_setting(monkeypatch):
    import src.settings as settings
    monkeypatch.setattr(settings, "get_setting", lambda k, d=None: True if k == "agent_subagent_reviewer" else d)
    assert st.parse_delegation_args('{"tasks": ["a"]}')["reviewer"] is True
    assert st.parse_delegation_args('{"tasks": ["a"], "reviewer": false}')["reviewer"] is False


# ── locks ──────────────────────────────────────────────────────────────────

def test_registry_claims_and_blocks(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    assert reg.claim("A", ["src/a.py", "src/b.py"]) == []
    assert reg.claim("B", ["src/b.py", "src/c.py"]) == ["src/b.py"]
    assert reg.blocked_by("B", ["src/a.py"]) == "A"
    assert reg.blocked_by("A", ["src/a.py"]) is None
    assert reg.blocked_by("A", [str(tmp_path / "src" / "c.py")]) == "B"   # absolute form, same file
    assert sorted(reg.owned_by("A")) == ["src/a.py", "src/b.py"]


def test_write_block_reason_only_inside_a_guarded_task(tmp_path):
    reg = st.FileLockRegistry(str(tmp_path))
    reg.claim("A", ["src/a.py"])
    assert st.write_block_reason("edit_file", json.dumps({"path": "src/a.py"})) is None  # no guard → no-op

    async def _as_worker_b():
        st._LOCK_CTX.set(st._LockGuard(reg, "B"))
        blocked = st.write_block_reason("edit_file", json.dumps({"path": "src/a.py", "old_string": "x", "new_string": "y"}))
        free = st.write_block_reason("write_file", json.dumps({"path": "src/new.py", "content": "x"}))
        st.note_write_result("write_file", json.dumps({"path": "src/new.py"}), {"output": "ok", "exit_code": 0})
        st.note_write_result("write_file", json.dumps({"path": "src/fail.py"}), {"error": "nope", "exit_code": 1})
        return blocked, free

    blocked, free = asyncio.run(_as_worker_b())
    assert blocked and "owned by sub-agent 'A'" in blocked and "src/a.py" in blocked
    assert free is None
    assert reg.owner[reg.norm("src/new.py")] == "B"          # first writer wins
    assert reg.norm("src/fail.py") not in reg.owner          # failed write claims nothing
    assert reg.conflicts == [{"worker": "B", "owner": "A", "path": "src/a.py"}]

    async def _as_reviewer():
        st._LOCK_CTX.set(st._LockGuard(reg, "reviewer", bypass=True))
        return st.write_block_reason("edit_file", json.dumps({"path": "src/a.py"}))
    assert asyncio.run(_as_reviewer()) is None


@pytest.mark.asyncio
async def test_dispatcher_refuses_a_locked_write(tmp_path, monkeypatch):
    """execute_tool_block returns the lock error instead of running the tool."""
    from src import tool_execution as te
    reg = st.FileLockRegistry(str(tmp_path))
    reg.claim("A", ["a.py"])
    st._LOCK_CTX.set(st._LockGuard(reg, "B"))
    try:
        ran = {"n": 0}

        async def _impl(block, **kw):
            ran["n"] += 1
            return ("ok", {"output": "written", "exit_code": 0})
        monkeypatch.setattr(te, "_execute_tool_block_impl", _impl)

        class _Block:
            tool_type = "write_file"
            content = json.dumps({"path": "a.py", "content": "x"})
        desc, result = await te.execute_tool_block(_Block(), workspace=str(tmp_path),
                                                   security_context=te.NO_TOOL_SECURITY_CONTEXT)
        assert result["locked"] is True and "owned by sub-agent 'A'" in result["error"] and ran["n"] == 0

        class _Free:
            tool_type = "write_file"
            content = json.dumps({"path": "b.py", "content": "x"})
        desc, result = await te.execute_tool_block(_Free(), workspace=str(tmp_path),
                                                   security_context=te.NO_TOOL_SECURITY_CONTEXT)
        assert result["output"] == "written" and ran["n"] == 1
        assert reg.owner[reg.norm("b.py")] == "B"
    finally:
        st._LOCK_CTX.set(None)


# ── runs: reviewer + stop ──────────────────────────────────────────────────

def _fake_loop(script):
    """stream_agent_loop stand-in: `script[name]` is a list of SSE events, or
    a coroutine factory for a worker that should block until cancelled."""
    async def _loop(endpoint_url, model, messages, **kwargs):
        name = messages[0]["content"].split("YOUR TASK: ", 1)[-1][:40]
        for key, events in script.items():
            if key in name:
                if callable(events):
                    await events()
                    return
                for ev in events:
                    yield ev
                return
        yield "data: [DONE]\n\n"
    return _loop


class _SM:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = type("S", (), {"messages": [], "add_message": lambda self, m: self.messages.append(m)})()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


def _harness_summary(mutations):
    return "data: " + json.dumps({"type": "harness_summary", "data": {"mutations": mutations, "stop_reason": "complete"}}) + "\n\n"


@pytest.mark.asyncio
async def test_reviewer_runs_after_the_workers_with_their_files(tmp_path, monkeypatch):
    import src.agent_loop as al
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: _SM())
    seen = []

    async def _loop(endpoint_url, model, messages, **kwargs):
        text = messages[0]["content"]
        seen.append(text)
        if "REVIEWER sub-agent" in text:
            yield _harness_summary(["src/a.py"])
        elif "task A" in text:
            yield _harness_summary(["src/a.py"])
        else:
            yield _harness_summary(["src/b.py"])
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", _loop)

    events = []

    async def _cb(payload):
        events.append(payload["subagent"])
    from src import tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:11434/v1", "model": "m", "headers": None, "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    tool = st.DelegateAgentsTool()
    result = await tool.execute(json.dumps({
        "tasks": [{"name": "A", "instruction": "task A", "files": ["src/a.py"]},
                  {"name": "B", "instruction": "task B", "files": ["src/b.py"]}],
        "reviewer": True, "parallel": True,
    }), {"session_id": "parent", "owner": None, "progress_cb": _cb})
    reps = result["subagents"]
    assert [r["role"] for r in reps] == ["worker", "worker", "reviewer"]
    assert reps[2]["name"] == "reviewer" and reps[2]["mutations"] == ["src/a.py"]
    reviewer_prompt = seen[-1]
    assert "REVIEWER sub-agent" in reviewer_prompt and "src/a.py (by A)" in reviewer_prompt and "src/b.py (by B)" in reviewer_prompt
    assert "FILES YOU OWN" in seen[0] and "src/a.py" in seen[0]
    assert "REVIEWER reviewer" in result["output"]
    started = [e for e in events if e["event"] == "started"]
    assert [e["role"] for e in started] == ["worker", "worker", "reviewer"]
    assert result["lock_conflicts"] == []


@pytest.mark.asyncio
async def test_stop_worker_cancels_only_that_worker(tmp_path, monkeypatch):
    import src.agent_loop as al
    import src.ai_interaction as ai
    gate = asyncio.Event()

    async def _loop(endpoint_url, model, messages, **kwargs):
        text = messages[0]["content"]
        if "slow" in text:
            await gate.wait()          # never set: only a cancel ends it
            yield "data: [DONE]\n\n"
        else:
            yield _harness_summary(["src/b.py"])
            yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", _loop)
    from src import tool_execution as te
    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:11434/v1", "model": "m", "headers": None, "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    events = []

    async def _cb(payload):
        events.append(payload["subagent"])
    tool = st.DelegateAgentsTool()
    task = asyncio.create_task(tool.execute(json.dumps({
        "tasks": [{"name": "slow", "instruction": "slow task"}, {"name": "fast", "instruction": "fast task"}],
        "parallel": True, "timeout_s": 60,
    }), {"session_id": "parent", "owner": None, "progress_cb": _cb}))
    # Wait until the slow worker has a child session registered, then stop it.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if st.active_worker_ids():
            break
    ids = st.active_worker_ids()
    assert ids, "worker was never registered"
    slow_sid = next(e["session_id"] for e in events if e["event"] == "started" and e["name"] == "slow")
    assert st.stop_worker(slow_sid) is True
    result = await asyncio.wait_for(task, 5)
    reps = {r["name"]: r for r in result["subagents"]}
    assert reps["slow"]["stop_reason"] == "stopped" and reps["slow"]["error"] is None
    assert reps["fast"]["stop_reason"] == "complete" and reps["fast"]["mutations"] == ["src/b.py"]
    assert st.stop_worker(slow_sid) is False       # already gone
    done = [e for e in events if e["event"] == "done" and e["name"] == "slow"]
    assert done and done[0]["stop_reason"] == "stopped"
