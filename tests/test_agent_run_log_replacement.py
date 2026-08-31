"""BUG 2 — the on-disk replay log is corrupted when a run is replaced.

`_log_path()` depends only on the session id, and `_RunLog.__init__` opens it
with mode "w" (truncate). When the user sends a second message (or edits and
resends), `start()` builds a NEW `_RunLog` for the same session while the OLD
one still holds an open descriptor at its own offset. Everything the old run
wrote afterwards — its remaining buffered events and, above all, its
`finish("stopped")` — landed INSIDE the new run's log, at the old run's offset.
Two failure modes:

  * `recover_interrupted_runs()` reads the LIVE run's log as terminal (or as
    unparsable garbage) and never recovers what it produced;
  * the recovered partial message is rebuilt with text from the CANCELLED run.

The fix orphans the old `_RunLog` at the moment of the replacement.

The tests deliberately push run 2's write offset PAST run 1's before letting
run 1's cancellation land — that is the real-world shape (the replacement run
streams for a while before the cancelled one finishes closing), and it is the
one where the old run's bytes land in the middle of the new run's file instead
of being harmlessly overwritten.
"""

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


async def _blocking_gen(gate):
    await gate.wait()
    yield "data: [DONE]\n\n"


# A non-delta event is flushed immediately; a delta is buffered until later.
def _flushed(payload: str) -> str:
    return "data: " + json.dumps({"type": "tool_output", "tool": "t", "output": payload}) + "\n\n"


def _delta(payload: str) -> str:
    return "data: " + json.dumps({"delta": payload}) + "\n\n"


async def _replaced_run_scenario(monkeypatch, old_text: str, new_chunks: list):
    """run 1 streams, run 2 replaces it and streams past run 1's offset, THEN
    run 1's cancellation lands. Returns (path, run1, run2)."""
    _settings(monkeypatch, agent_runs_persist=True, agent_runs_keep_hours=48)
    gate1, gate2 = asyncio.Event(), asyncio.Event()

    run1 = agent_runs.start("sid", _blocking_gen(gate1))
    await asyncio.sleep(0.02)
    assert run1.log is not None
    path = agent_runs._log_path("sid")
    for i in range(8):
        agent_runs._publish(run1, _flushed(f"old tool {i}"))
    offset1 = os.path.getsize(path)          # everything above is flushed
    agent_runs._publish(run1, _delta(old_text))   # buffered: flushed by finish()

    # The user sends another message.
    run2 = agent_runs.start("sid", _blocking_gen(gate2))
    for chunk in new_chunks:
        agent_runs._publish(run2, _delta(chunk))
        agent_runs._publish(run2, _flushed(chunk))
    assert os.path.getsize(path) > offset1, "run 2 must be ahead of run 1's offset"

    # Only now let run 1's cancellation (and its log.finish) run.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if run1.task.done():
            break
    assert run1.task.done()
    gate2.set()
    return path, run1, run2


async def _quiesce(*runs):
    for r in runs:
        for t in (r.task, r.evict_task):
            if t is not None and not t.done():
                t.cancel()
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_replaced_run_never_writes_into_the_new_runs_log(monkeypatch):
    chunks = [f"NEW-{i}" for i in range(20)]
    path, run1, run2 = await _replaced_run_scenario(monkeypatch, "OLD RUN TEXT", chunks)

    raw = open(path, "r", encoding="utf-8").read()
    assert "OLD RUN TEXT" not in raw, "the replaced run's buffered events leaked into the new run's log"
    for n, line in enumerate(raw.splitlines()):
        if line.strip():
            try:
                json.loads(line)
            except ValueError:
                pytest.fail(f"line {n} of the new run's log is torn: {line[:120]!r}")

    parsed = agent_runs._read_log(path)
    assert parsed["run_id"] == run2.run_id
    assert parsed["status"] == "running", "run 2 is still live but its log already reads as terminal"
    # Every event run 2 published is intact and in order.
    assert len(parsed["events"]) == len(run2.buffer)
    assert parsed["events"] == run2.buffer

    await _quiesce(run1, run2)


@pytest.mark.asyncio
async def test_recovery_rebuilds_the_live_run_not_the_replaced_one(monkeypatch):
    """End-to-end consequence: after a crash, recover_interrupted_runs() must
    rebuild the partial message of the run that was actually alive."""
    chunks = [f"[live-{i}]" for i in range(20)]
    path, run1, run2 = await _replaced_run_scenario(monkeypatch, "text from the cancelled run", chunks)

    saved = []

    class _Sess:
        id = "sid"

        def add_message(self, m):
            saved.append(m)

    class _SM:
        def get_session(self, sid):
            return _Sess()

        def save_sessions(self):
            pass

    recovered = agent_runs.recover_interrupted_runs(_SM())
    assert [r["session_id"] for r in recovered] == ["sid"], "the live run's log was not recovered"
    assert recovered[0]["run_id"] == run2.run_id
    assert saved, "no partial assistant message was rebuilt"
    body = saved[0].content
    assert "text from the cancelled run" not in body
    for c in chunks:
        assert c in body, f"the live run's output lost {c!r} to the replaced run's writes"

    await _quiesce(run1, run2)
