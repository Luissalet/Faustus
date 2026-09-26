"""Rolling prompt-cache breakpoints on the conversation for Claude."""
import copy

from src import llm_core


def _count(payload):
    return llm_core._count_anthropic_breakpoints(payload)


def _tools():
    return [{"type": "function", "function": {"name": "read_file", "description": "d",
                                               "parameters": {"type": "object", "properties": {}}}}]


def _history():
    img = [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
    return [
        {"role": "system", "content": "You are Faustus." * 10},
        {"role": "user", "content": img},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "file body"},
        {"role": "assistant", "content": "done reading"},
        {"role": "user", "content": "and now?"},
    ]


def test_agent_call_marks_newest_and_previous_round_within_limit():
    msgs = _history()
    before = copy.deepcopy(msgs)
    payload = llm_core._build_anthropic_payload("claude-sonnet-5", msgs, 0.2, 1000, tools=_tools())
    assert _count(payload) <= 4
    last = payload["messages"][-1]["content"]
    assert last[-1]["cache_control"] == {"type": "ephemeral"}
    # previous round ended at the tool result (a user message before an assistant one)
    marked_users = [m for m in payload["messages"] if m["role"] == "user"
                    and isinstance(m["content"], list) and any(b.get("cache_control") for b in m["content"])]
    assert len(marked_users) == 2
    # the caller's history is untouched
    assert msgs == before


def test_one_off_prompt_is_not_marked():
    payload = llm_core._build_anthropic_payload(
        "claude-sonnet-5", [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], 0.2, 100)
    assert _count(payload) == 0


def test_never_more_than_four_breakpoints_even_with_existing_marks():
    msgs = _history()
    msgs[1] = {"role": "user", "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}
    msgs[3] = {"role": "tool", "tool_call_id": "t1", "content": "file body"}
    payload = llm_core._build_anthropic_payload("claude-sonnet-5", msgs, 0.2, 1000, tools=_tools())
    assert _count(payload) <= 4


def test_openrouter_claude_gets_the_rolling_mark_without_touching_history():
    msgs = [{"role": "system", "content": "S" * 5000}, {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    payload = {"messages": msgs}
    original_last = msgs[-1]
    llm_core._apply_openrouter_anthropic_cache_hints(payload, tools=None)
    assert payload["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert original_last == {"role": "user", "content": "c"}
