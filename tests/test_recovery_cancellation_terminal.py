"""Observed stop during recovery is terminal for this invocation, no models."""
import asyncio
import json

import pytest
from src import agent_loop as al, agent_runs, llm_core
from tests.test_degenerate_output import _patch_common, _degenerate_error_chunk, _types


@pytest.mark.parametrize("clear_after_event", [False, True])
def test_recovery_stop_prevents_later_work_and_explicit_new_invocation_can_run(monkeypatch, clear_after_event):
    _patch_common(monkeypatch)
    session_id = "synthetic-recovery-cancel"
    run = agent_runs._Run()
    monkeypatch.setattr(agent_runs, "_RUNS", {session_id: run})
    effects, model_calls, ladder_calls, auxiliary_calls = [], [], [], []
    async def auxiliary(*a, **kw):
        auxiliary_calls.append(True)
        return "Synthetic auxiliary answer."
    monkeypatch.setattr(llm_core, "llm_call_async", auxiliary)
    async def execute(block, *a, **kw):
        effects.append(block.tool_type)
        return block.tool_type, {"output": "synthetic", "exit_code": 0}
    async def stream(*a, **kw):
        model_calls.append(True)
        if len(model_calls) > 1:
            yield 'data: {"delta":"Synthetic finished answer."}\n\n'
            yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
            yield 'data: [DONE]\n\n'
            return
        yield 'data: ' + json.dumps({"delta": "```bash\necho synthetic\n```"}) + '\n\n'
        yield _degenerate_error_chunk()
    async def ladder(*a, **kw):
        ladder_calls.append(True)
        run.cancel_requested = "task_cancelled"
        yield "result", {"ok": False, "cancelled": True}
    monkeypatch.setattr(al, "execute_tool_block", execute)
    monkeypatch.setattr(al, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(al, "_recovery_ladder", ladder)
    async def consume():
        chunks = []
        async for chunk in al.stream_agent_loop("http://unused/v1", "fixture",
                [{"role": "user", "content": "Run the synthetic task"}], max_rounds=1,
                relevant_tools={"bash"}, session_id=session_id,
                pending_cancel=lambda: agent_runs.is_cancel_requested(session_id)):
            chunks.append(chunk)
            if clear_after_event and '"type": "cancelled"' in chunk:
                run.cancel_requested = None
        return _types(chunks)
    events = asyncio.run(consume())
    assert ladder_calls == [True]
    assert any(event.get("type") == "cancelled" for event in events)
    assert effects == []
    assert len(model_calls) == 1
    assert auxiliary_calls == []
    assert not any(event.get("type") == "tool_start" for event in events)
    # An explicit new invocation may run after its caller clears cancellation.
    run.cancel_requested = None
    async def successful(*a, **kw):
        model_calls.append(True)
        yield 'data: {"delta":"Synthetic finished answer."}\n\n'
        yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(al, "stream_llm_with_fallback", successful)
    asyncio.run(consume())
    assert len(model_calls) == 2
