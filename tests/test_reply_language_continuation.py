"""The per-round reply-language reminder never splits a tool exchange.

Seen live on the 27B served by llama-server: the reminder was inserted
right before the last message, which after a tool round is the tool
result -- so the request read ``assistant(call) → user(reminder) →
tool(result)``, and the model repeated the identical call before
answering. These drive ``refresh_continuation`` exactly as the agent loop
does at the top of a round.
"""

from src.reply_language import refresh_continuation

HINT = {"role": "user", "content": "[Runtime requirement — reply language]\nResponde en español."}


def _call(i):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": f"c{i}", "type": "function",
                            "function": {"name": "plugins_list", "arguments": "{}"}}]}


def _result(i):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": "Writer app — NOT CONNECTED"}


def _roles(messages):
    return [m.get("_agent_injected") or m["role"] for m in messages]


def _assert_every_call_is_answered_next(messages):
    for i, m in enumerate(messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            following = messages[i + 1:i + 1 + len(ids)]
            assert [f.get("role") for f in following] == ["tool"] * len(ids), _roles(messages)
            assert [f.get("tool_call_id") for f in following] == ids


def test_after_a_tool_round_the_reminder_goes_before_the_call():
    messages = [{"role": "user", "content": "¿Qué aplicaciones mías puedes usar?"},
                _call(1), _result(1)]

    refresh_continuation(messages, HINT)

    assert _roles(messages) == ["user", "reply_language_continuity", "assistant", "tool"]
    _assert_every_call_is_answered_next(messages)


def test_parallel_results_stay_together():
    call = _call(1)
    call["tool_calls"].append({"id": "c2", "type": "function",
                               "function": {"name": "lookup_tools", "arguments": "{}"}})
    messages = [{"role": "user", "content": "hola"}, call, _result(1),
                {"role": "tool", "tool_call_id": "c2", "content": "{}"}]

    refresh_continuation(messages, HINT)

    _assert_every_call_is_answered_next(messages)
    assert messages[1].get("_agent_injected") == "reply_language_continuity"


def test_refreshing_every_round_keeps_one_reminder_and_whole_exchanges():
    messages = [{"role": "user", "content": "hola"}, _call(1), _result(1)]
    refresh_continuation(messages, HINT)
    messages += [_call(2), _result(2)]
    refresh_continuation(messages, HINT)

    assert _roles(messages).count("reply_language_continuity") == 1
    _assert_every_call_is_answered_next(messages)


def test_a_trailing_runtime_instruction_is_still_the_last_thing_read():
    # The reason the reminder goes BEFORE the last message at all: a pointed
    # instruction ("the tests FAILED, fix them") must stay last.
    messages = [{"role": "user", "content": "arregla el test"}, _call(1), _result(1),
                {"role": "user", "content": "The tests FAILED. Fix them."}]

    refresh_continuation(messages, HINT)

    assert messages[-1]["content"] == "The tests FAILED. Fix them."
    assert messages[-2].get("_agent_injected") == "reply_language_continuity"
    _assert_every_call_is_answered_next(messages)


def test_the_reminder_stays_put_while_little_follows_it():
    """Moving it every round re-read the previous round from the prompt
    cache; it moves only once a real stretch of work follows it."""
    messages = [{"role": "user", "content": "hola"}, _call(1), _result(1)]
    refresh_continuation(messages, HINT)
    first = [id(m) for m in messages]
    messages += [_call(2), _result(2)]
    before = list(messages)

    refresh_continuation(messages, HINT)

    assert messages == before and [id(m) for m in messages[:4]] == first
    assert sum(1 for m in messages if m.get("_agent_injected") == "reply_language_continuity") == 1


def test_the_reminder_moves_forward_after_a_long_stretch():
    from src.reply_language import KEEP_REMINDER_CHARS

    messages = [{"role": "user", "content": "hola"}, _call(1), _result(1)]
    refresh_continuation(messages, HINT)
    long_result = {"role": "tool", "tool_call_id": "c2", "content": "x" * (KEEP_REMINDER_CHARS + 1)}
    messages += [_call(2), long_result]

    refresh_continuation(messages, HINT)

    # The first copy stays where it was, so the prompt before the new one is
    # byte-for-byte the previous request's (the server's cache still holds it).
    assert _roles(messages) == ["user", "reply_language_continuity", "assistant", "tool",
                                "reply_language_continuity", "assistant", "tool"]
    _assert_every_call_is_answered_next(messages)


def test_a_changed_reminder_is_replaced_at_once():
    messages = [{"role": "user", "content": "hello"}, _call(1), _result(1)]
    refresh_continuation(messages, HINT)
    messages += [_call(2), _result(2)]
    other = {"role": "user", "content": "[Runtime requirement — reply language]\nAnswer in English."}

    refresh_continuation(messages, other)

    reminders = [m for m in messages if m.get("_agent_injected") == "reply_language_continuity"]
    assert len(reminders) == 1 and reminders[0]["content"] == other["content"]
    assert _roles(messages)[-3] == "reply_language_continuity"
