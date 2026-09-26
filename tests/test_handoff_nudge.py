"""A long turn is asked to record its progress before the wall-clock ceiling.

Exam 29: two 3-hour legs; the second began by re-reading everything the first
had established, because the first ended at the ceiling with nothing saved."""
import asyncio
import json
import time as real_time
import types

import src.agent_tools  # noqa: F401
from src import agent_loop as loop


class _Clock:
    """Real monotonic time plus an offset the first model call moves forward,
    as if that round had taken `elapsed` seconds."""

    def __init__(self, elapsed):
        self.elapsed = elapsed
        self.offset = 0.0

    def monotonic(self):
        return real_time.monotonic() + self.offset


def _drive(monkeypatch, elapsed, max_seconds=1000):
    captured = []

    clock = _Clock(elapsed)

    async def stream(candidates, messages, **kwargs):
        captured.append(list(messages))
        if len(captured) == 1:
            clock.offset += clock.elapsed          # the first round took that long
            yield "data: " + json.dumps({"delta": "```ls\n{\"path\": \".\"}\n```"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Sigo."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def execute(block, **kwargs):
        return block.tool_type, {"output": "a.txt", "exit_code": 0}
    monkeypatch.setattr(loop, "execute_tool_block", execute)
    fake_time = types.SimpleNamespace(**{k: getattr(real_time, k) for k in dir(real_time) if not k.startswith("_")})
    fake_time.monotonic = clock.monotonic
    monkeypatch.setattr(loop, "time", fake_time)
    monkeypatch.setattr(loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    settings = {"agent_turn_max_seconds": max_seconds, "agent_auto_continue_cycles": 0}
    monkeypatch.setattr(loop, "get_setting", lambda k, d=None: settings.get(k, d))

    async def run():
        return [c async for c in loop.stream_agent_loop(
            endpoint_url="http://127.0.0.1:11434/v1/chat/completions", model="qwen3.8:27b-q8_0",
            messages=[{"role": "user", "content": "Sigue con el análisis"}], headers={}, owner="admin",
            session_id="handoff", max_rounds=3, context_length=32768,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]
    events = "".join(asyncio.run(run()))
    return captured, events


def test_near_the_ceiling_the_model_is_asked_to_record_its_progress(monkeypatch):
    captured, events = _drive(monkeypatch, elapsed=900)
    assert "handoff_requested" in events
    assert len(captured) >= 2
    assert any("close to its time limit" in str(m.get("content")) for m in captured[1])


def test_early_in_the_turn_nothing_is_asked(monkeypatch):
    captured, events = _drive(monkeypatch, elapsed=100)
    assert "handoff_requested" not in events
    assert not any("close to its time limit" in str(m.get("content")) for msgs in captured for m in msgs)
