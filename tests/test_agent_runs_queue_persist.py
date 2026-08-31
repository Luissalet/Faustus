"""src.agent_runs: the task queue (lanes) and the on-disk replay log that
survives a restart (recover_interrupted_runs)."""

import asyncio
import json
import os

import pytest

from src import agent_runs


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._LANES.clear()
    agent_runs._INTERRUPTED.clear()


def _settings(monkeypatch, **values):
    monkeypatch.setattr(agent_runs, "_setting", lambda key, default=None: values.get(key, default), raising=False)


async def _gen(events, gate: asyncio.Event = None, delay=0.0):
    if gate is not None:
        await gate.wait()
    for ev in events:
        if delay:
            await asyncio.sleep(delay)
        yield ev


def _types(buffer):
    out = []
    for ev in buffer:
        if ev.startswith("data: ") and not ev.startswith("data: [DONE]"):
            try:
                d = json.loads(ev[6:])
            except ValueError:
                continue
            out.append((d.get("type"), d.get("position"), d.get("queued")))
    return out


@pytest.mark.asyncio
async def test_local_lane_runs_one_at_a_time_and_reports_positions(monkeypatch):
    _settings(monkeypatch, agent_queue_local_concurrency=1)
    gate = asyncio.Event()
    r1 = agent_runs.start("s1", _gen(['data: {"delta": "one"}\n\n', "data: [DONE]\n\n"], gate), lane="local", label="Chat 1")
    await asyncio.sleep(0.05)
    r2 = agent_runs.start("s2", _gen(['data: {"delta": "two"}\n\n', "data: [DONE]\n\n"]), lane="local", label="Chat 2")
    r3 = agent_runs.start("s3", _gen(['data: {"delta": "three"}\n\n', "data: [DONE]\n\n"]), lane="local", label="Chat 3")
    await asyncio.sleep(0.05)
    assert r1.queued_position == 0 and r2.queued_position == 1 and r3.queued_position == 2
    assert agent_runs.queued_positions() == {"s2": 1, "s3": 2}
    # The queued chats already know their position (replayable on reconnect)
    # and who is ahead of them.
    t2 = _types(r2.buffer)
    assert ("queue_status", 1, True) in t2
    ahead = json.loads(r3.buffer[0][6:])["ahead"]
    assert ahead == ["Chat 2"]
    assert r2.buffer == [ev for ev in r2.buffer if "queue_status" in ev]  # nothing else ran yet
    gate.set()
    for r in (r1, r2, r3):
        await asyncio.wait_for(r.task, 5)
    assert r1.status == r2.status == r3.status == "done"
    assert _types(r2.buffer)[-2:] == [("queue_status", 0, False), (None, None, None)]
    assert 'data: {"delta": "three"}\n\n' in r3.buffer
    assert agent_runs.queued_positions() == {}
    snap = agent_runs.queue_snapshot()["local"]
    assert snap["active"] == 0 and snap["waiting"] == []


@pytest.mark.asyncio
async def test_stopping_a_queued_run_leaves_the_queue(monkeypatch):
    _settings(monkeypatch, agent_queue_local_concurrency=1)
    gate = asyncio.Event()
    r1 = agent_runs.start("s1", _gen(['data: {"delta": "one"}\n\n', "data: [DONE]\n\n"], gate), lane="local")
    await asyncio.sleep(0.02)
    r2 = agent_runs.start("s2", _gen(['data: {"delta": "two"}\n\n', "data: [DONE]\n\n"]), lane="local")
    r3 = agent_runs.start("s3", _gen(['data: {"delta": "three"}\n\n', "data: [DONE]\n\n"]), lane="local")
    await asyncio.sleep(0.02)
    assert agent_runs.stop("s2", r2.run_id) is True
    await asyncio.sleep(0.05)
    assert r2.status == "stopped"
    assert agent_runs.queued_positions() == {"s3": 1}   # s3 moved up
    gate.set()
    await asyncio.wait_for(r1.task, 5)
    await asyncio.wait_for(r3.task, 5)
    assert r3.status == "done" and 'data: {"delta": "three"}\n\n' in r3.buffer


@pytest.mark.asyncio
async def test_api_lane_unlimited_by_default(monkeypatch):
    _settings(monkeypatch)
    gate = asyncio.Event()
    r1 = agent_runs.start("a1", _gen(['data: {"delta": "x"}\n\n', "data: [DONE]\n\n"], gate), lane=None)
    r2 = agent_runs.start("a2", _gen(['data: {"delta": "y"}\n\n', "data: [DONE]\n\n"]), lane=None)
    await asyncio.wait_for(r2.task, 5)
    assert r2.status == "done" and r1.status == "running"
    gate.set()
    await asyncio.wait_for(r1.task, 5)


@pytest.mark.asyncio
async def test_replay_log_is_written_and_finished(monkeypatch, tmp_path):
    _settings(monkeypatch, agent_runs_persist=True)
    r = agent_runs.start("log1", _gen([
        'data: {"delta": "he"}\n\n', 'data: {"delta": "llo"}\n\n',
        'data: {"type": "tool_output", "tool": "bash", "command": "ls", "output": "a.py", "exit_code": 0}\n\n',
        'data: {"type": "tool_progress", "tool": "bash", "round": 1, "elapsed": 1}\n\n',
        'data: {"type": "tool_progress", "tool": "bash", "round": 1, "elapsed": 2}\n\n',
        "data: [DONE]\n\n",
    ]))
    await asyncio.wait_for(r.task, 5)
    path = agent_runs._log_path("log1")
    assert os.path.isfile(path)
    info = agent_runs._read_log(path)
    assert info["status"] == "done" and info["run_id"] == r.run_id
    # Compacted progress ticks share a seq: the reader keeps the last one.
    assert len(info["events"]) == 5 and '"elapsed": 2' in info["events"][3]
    partial = agent_runs._partial_from_events(info["events"])
    assert partial["text"] == "hello" and partial["tool_events"][0]["tool"] == "bash"


@pytest.mark.asyncio
async def test_interrupted_run_is_recovered_into_the_chat(monkeypatch, tmp_path):
    _settings(monkeypatch, agent_runs_persist=True, agent_runs_keep_hours=48)
    gate = asyncio.Event()
    r = agent_runs.start("cut1", _gen([
        'data: {"delta": "Working on it"}\n\n',
        'data: {"type": "tool_output", "tool": "edit_file", "command": "{}", "output": "Edited a.py", "exit_code": 0}\n\n',
    ] , delay=0.0))
    await asyncio.sleep(0.1)
    # Simulate the process dying: forget the in-memory run, leave the log as is.
    if r.log is not None and r.log._f is not None:
        r.log._f.flush()
    r.task.cancel()
    agent_runs._RUNS.clear()
    path = agent_runs._log_path("cut1")
    # Rewrite the log without a terminal status (the cancel above wrote one).
    lines = [l for l in open(path, encoding="utf-8").read().splitlines() if '"status": "stopped"' not in l and '"status": "done"' not in l]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    class _Sess:
        def __init__(self):
            self.messages = []

        def add_message(self, m):
            self.messages.append(m)

    class _SM:
        def __init__(self):
            self.s = _Sess()
            self.saved = 0

        def get_session(self, sid):
            return self.s if sid == "cut1" else None

        def save_sessions(self):
            self.saved += 1

    sm = _SM()
    recovered = agent_runs.recover_interrupted_runs(sm)
    assert [e["session_id"] for e in recovered] == ["cut1"]
    assert recovered[0]["saved_message"] is True and sm.saved == 1
    msg = sm.s.messages[0]
    assert msg.role == "assistant" and msg.content.startswith("Working on it")
    assert agent_runs.INTERRUPTED_NOTE in msg.content
    assert msg.metadata["interrupted"] is True and msg.metadata["tool_events"][0]["tool"] == "edit_file"
    assert agent_runs.interrupted_runs()[0]["session_id"] == "cut1"
    # Marked interrupted on disk → a second startup does not re-save it.
    assert agent_runs._read_log(path)["status"] == "interrupted"
    assert agent_runs.recover_interrupted_runs(sm) == [] and sm.saved == 1
    assert agent_runs.acknowledge_interrupted("cut1") == 1 and agent_runs.interrupted_runs() == []


@pytest.mark.asyncio
async def test_old_finished_logs_are_pruned(monkeypatch, tmp_path):
    _settings(monkeypatch, agent_runs_persist=True, agent_runs_keep_hours=1)
    r = agent_runs.start("old1", _gen(["data: [DONE]\n\n"]))
    await asyncio.wait_for(r.task, 5)
    path = agent_runs._log_path("old1")
    old = os.path.getmtime(path) - 5 * 3600
    os.utime(path, (old, old))
    assert agent_runs.recover_interrupted_runs(None) == []
    assert not os.path.exists(path)
