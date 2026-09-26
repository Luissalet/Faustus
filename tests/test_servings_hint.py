import asyncio
import json

import pytest

import src.agent_tools  # noqa: F401
from src import agent_loop as loop
from src import answer_checks as ac


@pytest.mark.parametrize("text,people", [
    ("Hazme la lista de la compra para 8 personas", 8),
    ("Receta de lentejas para 6 comensales", 6),
    ("¿Cuántos kilos de pollo compro para 12 invitados?", 12),
    ("Give me a shopping list for 4 people", 4),
    ("Recipe that serves 10 guests", 10),
])
def test_servings_are_found(text, people):
    assert ac.servings_requested(text) == people


@pytest.mark.parametrize("text", [
    "Reserva mesa para 8 personas el viernes",      # no quantities asked
    "Lista de la compra para mí",                   # no number of people
    "Receta para 1 persona",                        # one person needs no scaling
])
def test_other_requests_get_no_hint(text):
    assert ac.servings_requested(text) is None


def test_the_loop_asks_for_the_per_person_arithmetic(monkeypatch):
    captured = []

    async def stream(candidates, messages, **kwargs):
        captured.append(list(messages))
        yield "data: " + json.dumps({"delta": "Lista lista."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)

    async def drive(text):
        return [c async for c in loop.stream_agent_loop(
            endpoint_url="http://127.0.0.1:11434/v1/chat/completions", model="qwen3.8:27b-q8_0",
            messages=[{"role": "user", "content": text}], headers={}, owner="admin", session_id="serv",
            max_rounds=1, context_length=32768,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]

    asyncio.run(drive("Hazme la lista de la compra para 8 personas: pollo al horno con patatas"))
    assert any("quantities for 8 people" in str(m.get("content")) for m in captured[0])
    captured.clear()
    asyncio.run(drive("¿Qué tiempo hará mañana en Madrid?"))
    assert not any("quantities for" in str(m.get("content")) for m in captured[0])
