"""Qwen3-family templates keep the current turn's reasoning themselves:
Faustus no longer strips it from earlier rounds (which changed an already
processed message and cost a prompt-cache miss every round). Other families
keep the old rule: only the newest assistant message carries reasoning."""
from src.agent_loop import _append_tool_results


def _round(messages, n, model):
    _append_tool_results(
        messages, "", [{"id": f"c{n}", "name": "read_file", "arguments": "{}"}],
        [{"output": "ok", "exit_code": 0}], ["ok"], True, n,
        round_reasoning=f"reasoning {n}", model=model,
    )


def _reasonings(messages):
    return [m.get("reasoning_content") for m in messages if m.get("role") == "assistant"]


def test_qwen3_keeps_every_round_of_the_turn():
    messages = [{"role": "user", "content": "q"}]
    for n in (1, 2, 3):
        _round(messages, n, "qwen3.8-27b-q8-llamacpp")
    assert _reasonings(messages) == ["reasoning 1", "reasoning 2", "reasoning 3"]


def test_other_models_keep_only_the_newest():
    messages = [{"role": "user", "content": "q"}]
    for n in (1, 2, 3):
        _round(messages, n, "nemotron-super")
    assert _reasonings(messages) == [None, None, "reasoning 3"]
