"""A greeting must not carry the whole toolset.

Measured on the local 27B, same question and same engine: 7.7 s with the
tools attached (7.5k of prompt), 3.0 s without them. The tool schemas are
most of what the engine reads before it can say a word, and on a pleasantry
nothing was ever going to call them.

The guard that matters is the other direction: a conversation that has
already used a tool keeps its tools even when the last message is a bare
"si", because there that word is an approval, not a pleasantry.
"""

from src.llm_core import _drop_tools_for_small_talk

TOOLS = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]


def _payload():
    return {"model": "qwen3", "tools": list(TOOLS), "messages": []}


def test_greeting_loses_the_toolset():
    payload = _payload()
    assert _drop_tools_for_small_talk(payload, [{"role": "user", "content": "hola"}]) is True
    assert "tools" not in payload


def test_work_request_keeps_the_toolset():
    payload = _payload()
    messages = [{"role": "user", "content": "arregla el test que falla en tests/test_x.py"}]
    assert _drop_tools_for_small_talk(payload, messages) is False
    assert payload["tools"] == TOOLS


def test_approval_after_a_tool_call_keeps_the_toolset():
    """"si" mid-task is a go-ahead, not small talk."""
    payload = _payload()
    messages = [
        {"role": "user", "content": "lista los ficheros"},
        {"role": "assistant", "tool_calls": [{"id": "1", "function": {"name": "ls"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "a.py"},
        {"role": "user", "content": "si"},
    ]
    assert _drop_tools_for_small_talk(payload, messages) is False
    assert payload["tools"] == TOOLS


def test_a_payload_without_tools_is_left_alone():
    payload = {"model": "qwen3", "messages": []}
    assert _drop_tools_for_small_talk(payload, [{"role": "user", "content": "hola"}]) is False
    assert "tools" not in payload


def test_tool_choice_goes_with_the_tools():
    payload = _payload()
    payload["tool_choice"] = "auto"
    assert _drop_tools_for_small_talk(payload, [{"role": "user", "content": "gracias"}]) is True
    assert "tool_choice" not in payload
