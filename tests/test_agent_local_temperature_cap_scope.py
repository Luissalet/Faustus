"""The local agent temperature cap follows coding intent, not workspace binding."""

import asyncio
import json

import pytest

import src.agent_loop as agent_loop


def _events(chunks):
    parsed = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            try:
                parsed.append(json.loads(chunk[6:]))
            except json.JSONDecodeError:
                pass
    return parsed


@pytest.mark.parametrize(
    ("endpoint", "user", "explicit", "initial", "expected", "capped_from", "cap"),
    [
        (
            "http://127.0.0.1:8081/v1",
            "Preview this CSV in the workspace and explain its columns.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Edit the Python module to fix the failing test.",
            False,
            0.6,
            0.4,
            0.6,
            0.4,
        ),
        (
            "http://127.0.0.1:8081/v1",
            "Edit the Python module to fix the failing test.",
            True,
            0.9,
            0.9,
            None,
            None,
        ),
        (
            "https://api.example.test/v1",
            "Edit the Python module to fix the failing test.",
            False,
            0.6,
            0.6,
            None,
            None,
        ),
    ],
    ids=("workspace-data-task", "local-coding", "explicit-temperature", "remote-coding"),
)
def test_stream_loop_temperature_cap_tracks_coding_intent(
    monkeypatch, tmp_path, endpoint, user, explicit, initial, expected, capped_from, cap
):
    """Exercise the real stream loop through its provider boundary, with no LLM."""
    captured = {}

    async def fake_provider(candidates, messages, **kwargs):
        captured.update(kwargs)
        yield 'data: {"delta":"Done."}\n\n'
        yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_provider)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: 0.4 if key == "agent_local_temperature_cap" else default)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())

    async def run():
        stream = agent_loop.stream_agent_loop(
            endpoint,
            "test-model",
            [{"role": "user", "content": user}],
            temperature=initial,
            temperature_explicit=explicit,
            workspace=str(tmp_path),
            max_rounds=1,
            context_length=32768,
            relevant_tools=set(),
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False},
        )
        return await _collect(stream)

    async def _collect(stream):
        return [chunk async for chunk in stream]

    events = _events(asyncio.run(run()))
    info = next(event for event in events if event.get("type") == "round_info")

    assert captured["temperature"] == expected
    assert info["temperature"] == expected
    assert info["temperature_capped_from"] == capped_from
    assert info["temperature_cap"] == cap
    # Preserve existing consumers of the legacy field.
    assert info["temperature_capped"] == capped_from
