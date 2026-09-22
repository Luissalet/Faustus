# -*- coding: utf-8 -*-
"""An explicit ask beats the small-talk whitelist.

The whitelist turns the reasoning off for a recognised pleasantry, which is
right by default and wrong the moment somebody says "think about this one".
`gen_overrides={"think": True}` is exactly that, and it is applied to the
payload before the whitelist runs -- so without this guard the heuristic
silently overruled the person who asked.

Found by the full suite: tests/test_llm_core_temperature_reasoning.py sends
"hi" (small talk by every rule in turn_effort) with think=True on purpose.
"""

from src.llm_core import _suppress_thinking_for_small_talk

MODEL = "qwen3.8-27b-q8-llamacpp"
GREETING = [{"role": "user", "content": "hola"}]


def test_a_plain_greeting_still_loses_its_reasoning():
    payload = {"model": MODEL}
    assert _suppress_thinking_for_small_talk(payload, MODEL, GREETING) is True


def test_enable_thinking_true_in_the_payload_is_left_alone():
    payload = {"model": MODEL, "chat_template_kwargs": {"enable_thinking": True}}
    assert _suppress_thinking_for_small_talk(payload, MODEL, GREETING) is False
    assert payload["chat_template_kwargs"] == {"enable_thinking": True}


def test_enable_thinking_false_is_not_an_explicit_ask():
    """Only a request TO think protects the turn; the default off does not."""
    payload = {"model": MODEL, "chat_template_kwargs": {"enable_thinking": False}}
    assert _suppress_thinking_for_small_talk(payload, MODEL, GREETING) is True


def test_a_native_think_flag_is_respected():
    payload = {"model": MODEL, "think": True}
    assert _suppress_thinking_for_small_talk(payload, MODEL, GREETING) is False


def test_a_reasoning_budget_is_respected():
    payload = {"model": MODEL, "reasoning_budget": 4096}
    assert _suppress_thinking_for_small_talk(payload, MODEL, GREETING) is False
