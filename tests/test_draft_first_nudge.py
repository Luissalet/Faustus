"""A task that must end in a file is asked to create it early (exam 29: six
hours, no answer file). Once, a while into the turn, only if nothing wrote it."""
import asyncio
import json
import time as real_time
import types

import src.agent_tools  # noqa: F401
from src import agent_loop as loop

REQUEST = "Analiza las imágenes de la carpeta y escribe la respuesta final en RESPUESTA.md"


class _Clock:
    def __init__(self):
        self.offset = 0.0

    def monotonic(self):
        return real_time.monotonic() + self.offset


def _drive(monkeypatch, workspace, elapsed=300):
    captured = []
    clock = _Clock()

    async def stream(candidates, messages, **kwargs):
        captured.append(list(messages))
        if len(captured) == 1:
            clock.offset += elapsed
            yield "data: " + json.dumps({"delta": "```ls\n{\"path\": \".\"}\n```"}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": "Sigo."}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def execute(block, **kwargs):
        return block.tool_type, {"output": "img1.png", "exit_code": 0}

    fake_time = types.SimpleNamespace(**{k: getattr(real_time, k) for k in dir(real_time) if not k.startswith("_")})
    fake_time.monotonic = clock.monotonic
    monkeypatch.setattr(loop, "time", fake_time)
    monkeypatch.setattr(loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(loop, "execute_tool_block", execute)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    settings = {"agent_turn_max_seconds": 1000, "agent_auto_continue_cycles": 0}
    monkeypatch.setattr(loop, "get_setting", lambda k, d=None: settings.get(k, d))

    async def run():
        return [c async for c in loop.stream_agent_loop(
            endpoint_url="http://127.0.0.1:11434/v1/chat/completions", model="qwen3.8:27b-q8_0",
            messages=[{"role": "user", "content": REQUEST}], headers={}, owner="admin",
            session_id="draft", max_rounds=3, context_length=32768, workspace=str(workspace),
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]
    events = "".join(asyncio.run(run()))
    return captured, events


def test_the_answer_file_is_asked_for_a_while_into_the_turn(monkeypatch, tmp_path):
    captured, events = _drive(monkeypatch, tmp_path)
    assert "draft_first_requested" in events
    assert any("RESPUESTA.md does not exist yet" in str(m.get("content")) for msgs in captured[1:] for m in msgs)


def test_an_existing_answer_file_is_not_asked_for(monkeypatch, tmp_path):
    (tmp_path / "RESPUESTA.md").write_text("# Respuesta\n", encoding="utf-8")
    captured, events = _drive(monkeypatch, tmp_path)
    assert "draft_first_requested" not in events


def test_early_in_the_turn_nothing_is_asked(monkeypatch, tmp_path):
    captured, events = _drive(monkeypatch, tmp_path, elapsed=10)
    assert "draft_first_requested" not in events
