""""Continúa" after a turn that stopped at its limit keeps that turn's tool work.

History replays only the assistant's text; the previous turn's tool calls and
results live in its metadata. Exam 29's second leg started by re-reading the
whole transcription the first leg had already read."""
import asyncio
import json

import src.agent_tools  # noqa: F401
from src import agent_loop as loop


def _drive(monkeypatch, history):
    captured = []

    async def stream(candidates, messages, **kwargs):
        captured.append(list(messages))
        yield "data: " + json.dumps({"delta": "Sigo con el cuadro 3."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)

    async def run():
        return [c async for c in loop.stream_agent_loop(
            endpoint_url="http://127.0.0.1:11434/v1/chat/completions", model="qwen3.8:27b-q8_0",
            messages=history, headers={}, owner="admin", session_id="carry", max_rounds=1,
            context_length=65536,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]
    asyncio.run(run())
    return captured[0]


PREVIOUS = {
    "role": "assistant",
    "content": "He leído la transcripción y los cuadros 1 y 2. Me he quedado sin tiempo.",
    "metadata": {"tool_events": [
        {"tool": "read_file", "command": '{"path": "transcripcion.md"}',
         "output": "Cuadro 1: la cena. Cuadro 2: el puerto de 1540.", "exit_code": 0},
    ]},
}


def test_continue_brings_the_previous_turns_results(monkeypatch):
    sent = _drive(monkeypatch, [
        {"role": "user", "content": "Analiza los cuadros de la carpeta"}, PREVIOUS,
        {"role": "user", "content": "continúa"},
    ])
    notes = [str(m.get("content")) for m in sent if "in the previous turn" in str(m.get("content"))]
    assert notes and "el puerto de 1540" in notes[0]


def test_a_new_request_does_not_drag_old_results_along(monkeypatch):
    sent = _drive(monkeypatch, [
        {"role": "user", "content": "Analiza los cuadros de la carpeta"}, PREVIOUS,
        {"role": "user", "content": "¿Qué tiempo hace hoy en Madrid?"},
    ])
    assert not any("in the previous turn" in str(m.get("content")) for m in sent)
