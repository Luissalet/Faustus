"""A05 — acceptance-parity case.

Contract (`docs/spec/paridad/acceptance_cases.json`, A05): a tool call
whose `effect_class` is not `"read"` must record `pending` BEFORE dispatch
and `confirmed`/`failed` after — through the real `execute_tool_block`, not
a simulation of it — so that a process death between those two writes (the
process itself is never killed here; the coroutine is cancelled after the
`pending` write is already on disk, which is the same observable state a
real crash leaves) is recoverable as an `unknown_effect`, not silently
forgotten or silently retried.

Three things are proven, in one continuous scenario:

1. `src.tool_execution.execute_tool_block` writes the `pending` `tool_effect`
   event through `src.agent_runs.record_tool_effect`, flushed to the run's
   on-disk log immediately (not batched with prose deltas), for a real
   non-read tool (`write_file`).
2. `src.agent_runs.recover_interrupted_runs` turns a `pending` with no
   matching `confirmed`/`failed` into `metadata.unknown_effects` on the
   recovered partial message, and the saved note names the call.
3. `src.agent_runs.unknown_effects_system_block`, wired into
   `src.chat_processor.ChatProcessor.build_context_preface` at the same
   preface slot as the behaviour-mode block, surfaces that list to the NEXT
   turn's system preface — the "do not repeat them without checking" line a
   fake LLM's next call would actually see.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.models import ChatMessage
from src import agent_runs, tool_execution
from src.agent_tools import ToolBlock
from src.chat_processor import ChatProcessor
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block

SESSION_ID = "a05-session"
CALL_ID = "call_1_0"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path / "data"), raising=False)
    agent_runs._RUNS.clear()
    agent_runs._INTERRUPTED.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._INTERRUPTED.clear()


class _Memory:
    def load(self, owner=None):
        return []


class _Docs:
    rag_manager = None


class _Sess:
    def __init__(self):
        self.history = []
        self.messages = self.history  # some call sites use .messages

    def add_message(self, m):
        self.history.append(m)


class _SM:
    def __init__(self, sess):
        self._sess = sess
        self.saved = 0

    def get_session(self, sid):
        return self._sess if sid == SESSION_ID else None

    def save_sessions(self):
        self.saved += 1


async def _hang_forever(*_args, **_kwargs):
    """Stands in for `_execute_tool_block_impl`: the effect has "started"
    (execute_tool_block already wrote the `pending` tool_effect event before
    calling this) and now never returns -- the same observable state a
    process death between "the write happened" and "the result was
    recorded" leaves, without actually killing the test process."""
    await asyncio.Event().wait()


async def _turn_generator(hang_started: asyncio.Event):
    yield 'data: {"delta": "Writing the file now."}\n\n'
    block = ToolBlock(tool_type="write_file",
                      content=json.dumps({"path": "notes/a05.txt", "content": "hi"}))
    hang_started.set()
    await execute_tool_block(
        block, session_id=SESSION_ID, owner="luis", workspace="",
        security_context=NO_TOOL_SECURITY_CONTEXT, call_id=CALL_ID,
    )
    yield 'data: {"delta": "done"}\n\n'  # never reached -- the call above hangs


@pytest.mark.acceptance("A05")
@pytest.mark.asyncio
async def test_a_pending_effect_that_never_resolves_recovers_as_unknown_effect_and_reaches_the_next_turn(
    monkeypatch,
):
    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", _hang_forever)

    hang_started = asyncio.Event()
    run = agent_runs.start(SESSION_ID, _turn_generator(hang_started))
    await asyncio.wait_for(hang_started.wait(), timeout=5)
    # Give execute_tool_block's own coroutine a beat past `hang_started.set()`
    # to actually reach and complete the pending write before the effect
    # dispatch call, which then hangs.
    await asyncio.sleep(0.05)

    # ---- 1. the pending write happened, for real, flushed immediately ----
    assert run.log is not None
    with open(run.log.path, encoding="utf-8") as f:
        raw_lines = f.read().splitlines()
    pending_events = []
    for line in raw_lines:
        obj = json.loads(line)
        ev = obj.get("ev")
        if not isinstance(ev, str) or not ev.startswith("data: "):
            continue
        payload = json.loads(ev[len("data: "):])
        if payload.get("type") == "tool_effect":
            pending_events.append(payload)
    assert len(pending_events) == 1, pending_events
    effect = pending_events[0]
    assert effect["call_id"] == CALL_ID
    assert effect["tool"] == "write_file"
    assert effect["effect_class"] == "write"
    assert effect["state"] == "pending"
    assert effect["idempotency_key"] == f"{run.run_id}:{CALL_ID}"
    # Immediate flush, not batched with the prose deltas: this line landed
    # on disk before any deliberate flush of the surrounding deltas would
    # have happened (the `finish()` call below is the first explicit one).
    assert raw_lines  # sanity: something was written at all

    # ---- simulate the crash: cancel the coroutine, forget the in-memory run ----
    run.task.cancel()
    try:
        await run.task
    except asyncio.CancelledError:
        pass
    agent_runs._RUNS.clear()
    # The cancellation path still writes a terminal status line (stopped) --
    # a REAL process death would not have. Strip it so the log reads exactly
    # like the "still running" state recover_interrupted_runs is built for,
    # same technique tests/test_agent_runs_queue_persist.py uses.
    kept = [l for l in raw_lines if '"status": "stopped"' not in l
            and '"status": "done"' not in l and '"status": "error"' not in l]
    with open(run.log.path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")

    # ---- 2. recovery surfaces the unresolved effect ----
    sess = _Sess()
    sm = _SM(sess)
    recovered = agent_runs.recover_interrupted_runs(sm)
    assert [r["session_id"] for r in recovered] == [SESSION_ID]
    assert recovered[0]["unknown_effects"] == [
        {"call_id": CALL_ID, "tool": "write_file",
         "idempotency_key": f"{run.run_id}:{CALL_ID}"}
    ]
    assert sm.saved == 1
    msg = sess.history[0]
    assert msg.role == "assistant"
    assert msg.content.startswith("Writing the file now.")
    assert agent_runs.INTERRUPTED_NOTE in msg.content
    assert "NOT retried" in msg.content
    assert "write_file" in msg.content and CALL_ID in msg.content
    assert msg.metadata["unknown_effects"] == recovered[0]["unknown_effects"]

    # A second recovery pass must not re-report it (the log is now marked
    # interrupted) -- same invariant test_agent_runs_queue_persist.py checks.
    assert agent_runs.recover_interrupted_runs(sm) == [] and sm.saved == 1

    # ---- 3. the next turn's preface carries the "do not repeat" line ----
    block = agent_runs.unknown_effects_system_block(sess)
    assert block is not None
    assert "write_file" in block and CALL_ID in block
    assert f"{run.run_id}:{CALL_ID}" in block

    processor = ChatProcessor(memory_manager=_Memory(), personal_docs_manager=_Docs())
    from types import SimpleNamespace
    preface, _rag, _web = processor.build_context_preface(
        message="continue", session=SimpleNamespace(), use_rag=False, use_memory=False,
        preset_system_prompt="PRESET", unknown_effects_block=block,
    )
    roles_content = [(m["role"], m["content"]) for m in preface[:3]]
    assert roles_content[0] == ("system", "PRESET")
    assert roles_content[1] == ("system", block)
    assert roles_content[2] == ("system", UNTRUSTED_CONTEXT_POLICY)
    # The fake LLM's next call receives exactly this preface as its system
    # context -- this list IS what "continue" would send it.
    assert any("write_file" in m["content"] for m in preface if m["role"] == "system")

    # ---- the turn AFTER that: no crash left behind, so the line disappears ----
    sess.add_message(ChatMessage("assistant", "Wrote the file. Anything else?"))
    assert agent_runs.unknown_effects_system_block(sess) is None


@pytest.mark.acceptance("A05")
@pytest.mark.asyncio
async def test_a_read_only_tool_never_writes_a_tool_effect_event(monkeypatch):
    """Herramientas `read` no generan eventos -- proven against the real
    classifier, not asserted from the contract text alone."""
    async def _quick_read(*_a, **_kw):
        return ("read_file: ok", {"content": "hi", "exit_code": 0})

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", _quick_read)

    async def gen():
        block = ToolBlock(tool_type="read_file", content=json.dumps({"path": "x.txt"}))
        await execute_tool_block(
            block, session_id=SESSION_ID, owner="luis", workspace="",
            security_context=NO_TOOL_SECURITY_CONTEXT, call_id="call_read",
        )
        yield 'data: {"delta": "done"}\n\n'

    run = agent_runs.start(SESSION_ID, gen())
    for _ in range(50):
        if run.status != "running":
            break
        await asyncio.sleep(0.02)
    assert not any(
        json.loads(ev[6:]).get("type") == "tool_effect"
        for ev in run.buffer if ev.startswith("data: ") and not ev.startswith("data: [DONE]")
    )
