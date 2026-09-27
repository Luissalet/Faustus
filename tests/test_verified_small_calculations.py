"""Real 3B failures: a letter count and a shared bill with a tip."""
import asyncio
import json

import pytest

import src.agent_tools  # noqa: F401
from src import agent_loop, answer_checks


@pytest.mark.parametrize("question,expected,wrong,right", [
    ("¿Cuántas veces aparece la letra r en la palabra ferrocarrilero?",
     "5", "1", "Hay 5 letras r."),
    ("Propina del 15 % sobre una cuenta de 86,40 €. Somos 4: ¿cuánto paga cada uno con la propina incluida?",
     "24.84", "Cada uno paga 99,36 €.", "Cada uno paga 24,84 €."),
])
def test_verifiable_result_and_mismatch(question, expected, wrong, right):
    assert answer_checks.verified_calculation(question)["value"] == expected
    assert answer_checks.calculation_mismatch(question, wrong)
    assert answer_checks.calculation_mismatch(question, right) is None


@pytest.mark.parametrize("question", [
    "¿Cuánto es el 15 % de 86,40 €?",  # no per-person request
    "Somos 4 y la cuenta es de 86,40 €, ¿qué hacemos?",  # no tip percentage
    "¿Cuántas veces aparece esta palabra en el documento?",  # no literal word
])
def test_ambiguous_calculations_are_left_to_the_model(question):
    assert answer_checks.verified_calculation(question) is None


def test_links_count_requires_the_all_state_data_result():
    question = "Usa Links Hoard para consultar cuántos enlaces tengo guardados."
    assert answer_checks.saved_links_count_requested(question)
    assert not answer_checks.saved_links_count_requested("¿Qué es Links Hoard?")
    catalog = {"tool": "lookup_tools", "command": "{}", "output": '{"tools":[]}'}
    unread = {"tool": "mcp__123__list_links", "command": '{"state":"unread"}',
              "output": '{"total":0,"items":[]}', "exit_code": 0}
    all_links = {"tool": "mcp__123__list_links", "command": '{"state":"all","limit":1}',
                 "output": '{"total":29,"items":[]}', "exit_code": 0}
    assert answer_checks.saved_links_total([catalog, unread]) is None
    assert answer_checks.saved_links_total([catalog, unread, all_links]) == 29
    assert answer_checks.saved_links_total([{**all_links, "exit_code": 1}]) is None


def test_agent_rewrites_a_wrong_count_before_showing_it(monkeypatch):
    calls = []

    async def stream(candidates, messages, **kwargs):
        calls.append(list(messages))
        yield "data: " + json.dumps({"delta": "1" if len(calls) == 1 else "5"}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)

    async def drive():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            endpoint_url="http://127.0.0.1:11434/v1/chat/completions", model="qwen3.8:27b-q8_0",
            messages=[{"role": "user", "content": "¿Cuántas veces aparece la letra r en la palabra ferrocarrilero?"}],
            headers={}, owner="admin", session_id="verified-count", max_rounds=2,
            context_length=32768,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]

    events = asyncio.run(drive())
    assert len(calls) == 2
    assert any("Verified calculation" in str(m.get("content")) for m in calls[0])
    assert any('"wrong_calculation"' in chunk for chunk in events)
    assert any("contains 5" in str(m.get("content")) or "contiene 5" in str(m.get("content"))
               for m in calls[1])


def test_agent_falls_back_to_verified_result_when_rewrite_is_still_wrong(monkeypatch):
    calls = []

    async def stream(candidates, messages, **kwargs):
        calls.append(list(messages))
        yield "data: " + json.dumps({"delta": "Cada uno paga 99,36 €."}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10)

    async def drive():
        return [chunk async for chunk in agent_loop.stream_agent_loop(
            endpoint_url="http://127.0.0.1:11434/v1/chat/completions", model="qwen3.8:27b-q8_0",
            messages=[{"role": "user", "content": "Propina del 15 % sobre una cuenta de 86,40 €. Somos 4: ¿cuánto paga cada uno con la propina incluida?"}],
            headers={}, owner="admin", session_id="verified-tip", max_rounds=2,
            context_length=32768,
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]

    events = asyncio.run(drive())
    replacements = [json.loads(line[6:]) for chunk in events for line in chunk.splitlines()
                    if line.startswith("data: {") and '"response_replace"' in line]
    assert len(calls) == 2
    assert replacements[-1]["text"].endswith("24,84 € por persona.")
