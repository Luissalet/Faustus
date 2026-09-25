"""A Qwen3 tool call that llama.cpp left at the end of reasoning_content is
recovered (llama.cpp issues #20809, #22684); one drafted mid-thought is not."""
from src.agent_loop import _resolve_tool_blocks


CALL = '<tool_call>\n{"name": "web_search", "arguments": {"query": "tiempo Madrid"}}\n</tool_call>'


def test_a_call_at_the_end_of_the_reasoning_runs():
    blocks, used_native, _ = _resolve_tool_blocks("", [], 1, is_api_model=True,
                                                  round_reasoning="Busco el tiempo.\n" + CALL)
    assert [b.tool_type for b in blocks] == ["web_search"] and used_native is False


def test_a_call_drafted_mid_thought_does_not():
    reasoning = "Podría usar " + CALL + " pero mejor respondo directamente."
    blocks, _, _ = _resolve_tool_blocks("", [], 1, is_api_model=True, round_reasoning=reasoning)
    assert blocks == []


def test_a_round_with_text_is_left_alone():
    blocks, _, _ = _resolve_tool_blocks("Hace sol.", [], 1, is_api_model=True, round_reasoning=CALL)
    assert blocks == []
