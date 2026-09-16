"""A29 — bounded, deterministic loop escalation, independent of the model's
own self-reflection.

`src/loop_breaker.py` (this lot) is a pure, fully-tested policy: N identical
(tool, args, result) calls nudge, another M block the tool, another K stop
the turn with stop_reason ``non_progressing_loop`` (tests/test_loop_breaker.py
exercises the policy itself in isolation, with no LLM and no agent loop).

`src/agent_loop.py` is NOT owned by this lot (docs/spec/paridad/CONTRATO.md,
"Propiedad de ficheros"): only T4 may touch it, and only inside the
tool-selection functions. Today's inline loop recovery there
(`_loop_recovery_*`, FAUSTUS.md §87) redirects and blocks a repeated tool,
but never actually STOPS the turn with a `non_progressing_loop` reason — it
keeps cycling the model through "try something else" indefinitely. Wiring
`LoopPolicy` into the real round loop is therefore real, necessary work this
lot cannot do without touching a file it does not own.

This test drives the REAL `src.agent_loop.stream_agent_loop` (only the LLM
stream and tool execution are faked — same harness as
`tests/test_agent_harness_loop.py`) with a model that repeats the identical
tool call 20 times running. It is the acceptance test A29 actually needs, and
it is expected to FAIL against today's `agent_loop.py`: nothing there yet
stops a turn independently for a non-progressing loop, or reports
`non_progressing_loop`. It is marked `xfail(strict=True)` — if it ever starts
passing, that means the wiring in `T7_wiring.md` (or something equivalent)
landed, and this test should be turned into a normal, green acceptance test
at that point rather than staying xfail.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import src.agent_loop as al

from tests.acceptance.conftest import record_evidence


def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


@pytest.mark.acceptance("A29")
@pytest.mark.xfail(
    strict=True,
    reason=(
        "src/loop_breaker.py's LoopPolicy is implemented and unit-tested "
        "(tests/test_loop_breaker.py) but not wired into src/agent_loop.py, "
        "which this lot (T7) does not own outside tool-selection functions "
        "(CONTRATO.md). See T7_wiring.md for the exact diff. Today's inline "
        "_loop_recovery_* mechanism redirects/blocks a repeated call but "
        "never stops the turn with stop_reason=non_progressing_loop, so this "
        "real end-to-end run keeps cycling instead of stopping."
    ),
)
def test_twenty_identical_tool_calls_stop_the_turn_deterministically(tmp_path, monkeypatch, request):
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)
    monkeypatch.setattr(al, "blocked_tools_for_owner", lambda owner: set(), raising=False)

    # The SAME tool call, SAME arguments, and (crucially) the SAME result
    # every single round — no progress at all, 20 rounds running.
    async def _fake_exec(block, *a, **k):
        return (block.tool_type, {"output": "still nothing found", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    identical_call = "```bash\nls -la /nonexistent\n```"
    calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        yield f'data: {json.dumps({"delta": identical_call})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "finish_reason": "tool_calls"})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)

    gen = al.stream_agent_loop(
        "http://127.0.0.1:11434/v1", "qwen3-coder:30b",
        [{"role": "user", "content": "Find the config file and read it"}],
        max_rounds=20, relevant_tools={"bash"}, workspace=str(tmp_path),
    )
    events = _events(_collect(gen))

    # What A29 expects: the turn stops on its own, within a SMALL, bounded
    # number of rounds (not all 20 available ones), with a deterministic
    # motive — never by the max_rounds ceiling, and never because the model
    # "decided" to stop.
    summaries = [e for e in events if e.get("type") == "harness_summary"]
    stop_reason = (summaries[-1]["data"].get("stop_reason") if summaries else None)
    record_evidence(request, module="src.loop_breaker", real_call_count=calls["n"], events_stop_reason=stop_reason)
    assert calls["n"] <= 10, f"turn ran {calls['n']} identical rounds without stopping"
    assert stop_reason == "non_progressing_loop", stop_reason
