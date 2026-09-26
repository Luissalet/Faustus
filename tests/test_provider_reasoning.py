"""Reasoning in each provider's own words (src/provider_reasoning.py)."""
from src import provider_reasoning as pr


def test_claude_versions():
    assert pr.claude_version("claude-opus-4-7-20260201") == ("opus", 4, 7)
    assert pr.claude_version("claude-sonnet-5") == ("sonnet", 5, 0)
    assert pr.claude_version("claude-3-7-sonnet-20250219") == ("sonnet", 3, 7)
    assert pr.claude_version("claude-haiku-4-5") == ("haiku", 4, 5)
    assert pr.claude_version("gpt-5.5") is None


def test_capabilities_follow_the_model_line():
    assert pr.anthropic_capabilities("claude-opus-5-5")["efforts"] == ("low", "medium", "high", "max", "xhigh")
    assert pr.anthropic_capabilities("claude-opus-5-5")["adaptive"] is True
    sonnet46 = pr.anthropic_capabilities("claude-sonnet-4-6")
    assert "max" in sonnet46["efforts"] and "xhigh" not in sonnet46["efforts"]
    haiku = pr.anthropic_capabilities("claude-haiku-4-5")
    assert haiku["efforts"] == () and haiku["adaptive"] is False and haiku["thinking"] is True
    assert pr.anthropic_capabilities("claude-fable-5-1")["efforts"][-1] == "xhigh"


def test_adaptive_thinking_and_effort_on_new_claude():
    payload = {"model": "claude-opus-5-5", "max_tokens": 4096, "temperature": 0.3}
    assert pr.apply_anthropic(payload, "claude-opus-5-5",
                              {"think": True, "reasoning_effort": "xhigh", "reasoning_budget": 16384})
    assert payload["thinking"] == {"type": "adaptive"}
    assert payload["output_config"] == {"effort": "xhigh"}
    assert payload["max_tokens"] == 4096 + 16384 and "temperature" not in payload


def test_budget_thinking_on_older_claude_and_effort_fit():
    payload = {"max_tokens": 2000, "temperature": 0.5}
    pr.apply_anthropic(payload, "claude-haiku-4-5", {"think": True, "reasoning_effort": "max", "reasoning_budget": 8192})
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert payload["max_tokens"] > 8192 and "output_config" not in payload
    p2 = {"max_tokens": 1000}
    pr.apply_anthropic(p2, "claude-sonnet-4-6", {"think": True, "reasoning_effort": "xhigh"})
    assert p2["output_config"] == {"effort": "max"}


def test_tool_loops_get_effort_without_thinking():
    payload = {"max_tokens": 4096, "temperature": 0.2}
    pr.apply_anthropic(payload, "claude-opus-5-5", {"think": True, "reasoning_effort": "high"}, allow_thinking=False)
    assert "thinking" not in payload and payload["output_config"] == {"effort": "high"}
    assert payload["temperature"] == 0.2


def test_nothing_asked_changes_nothing():
    payload = {"max_tokens": 10}
    assert not pr.apply_anthropic(payload, "claude-opus-5-5", {})
    assert not pr.apply_anthropic(payload, "claude-opus-5-5", {"think": False})
    assert payload == {"max_tokens": 10}


def test_openrouter_reasoning_object():
    payload = {}
    assert pr.apply_openrouter(payload, {"think": True, "reasoning_effort": "max"})
    assert payload["reasoning"] == {"effort": "max"}
    p2 = {}
    pr.apply_openrouter(p2, {"think": True})
    assert p2["reasoning"] == {"effort": "high"}
    assert not pr.apply_openrouter({}, {"think": False})


def test_fit_effort_and_strip():
    assert pr.fit_effort("xhigh", ("low", "medium", "high")) == "high"
    assert pr.fit_effort("minimal", ("low", "high")) == "low"
    assert pr.fit_effort("max", ("low", "high", "xhigh")) == "xhigh"
    payload = {"reasoning_effort": "xhigh", "thinking": {}, "chat_template_kwargs": {"enable_thinking": True}}
    assert pr.strip_reasoning(payload)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}
    assert pr.looks_like_reasoning_error(400, "Unsupported value: 'xhigh' for reasoning_effort")
    assert not pr.looks_like_reasoning_error(500, "reasoning")
