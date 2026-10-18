from src import engine_controls as controls
from src.reasoning_levels import levels_for


def test_controls_require_model_and_engine(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: False)
    assert controls.controls_for("http://configured/v1", "glm-5.3-flash-nvfp4") is None
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    assert controls.controls_for("http://configured/v1", "glm-4") is None
    assert controls.controls_for("http://configured/v1", "qwen3.8") is None
    result = levels_for("http://configured/v1", "nvidia/GLM-5.3-Flash-NVFP4")
    assert result["levels"] == ["low", "high", "max"]
    assert result["thinking_toggle"] is True


def test_fast_mode_uses_supported_low_effort_and_keeps_explicit_budget(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"enable_thinking": False},
               "reasoning_effort": "high", "reasoning_budget": 4096}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"] == 4096
    assert payload["reasoning_effort"] == "low"
    assert "reasoning_effort" not in payload["chat_template_kwargs"]
    assert payload["chat_template_kwargs"]["enable_thinking"] is True
    assert "reasoning_budget" not in payload
    payload["chat_template_kwargs"]["enable_thinking"] = True
    payload["reasoning_budget"] = 128
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"] == 128


def test_missing_budget_does_not_invent_a_cap_or_overwrite_native_budget(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload={}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert "thinking_token_budget" not in payload
    payload={"thinking_token_budget":128}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"]==128
    assert payload["chat_template_kwargs"]["clear_thinking"] is True


def test_unlimited_and_explicit_template_choice_survive(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload={"reasoning_budget":-1,"chat_template_kwargs":{"clear_thinking":False}}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"]==-1
    assert payload["chat_template_kwargs"]["clear_thinking"] is False


def test_fast_mode_without_explicit_budget_never_forces_empty_reasoning(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload={"reasoning_effort":"high","chat_template_kwargs":{"enable_thinking":False,"reasoning_effort":"high"}}
    controls.apply_controls(payload,"http://configured/v1","glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"]=="low"
    assert "reasoning_effort" not in payload["chat_template_kwargs"]
    assert "thinking_token_budget" not in payload


def test_unsupported_effort_never_silently_reaches_template(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"reasoning_effort": "xhigh"}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "max"


def test_native_template_effort_is_fallback_but_top_level_wins(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"reasoning_effort": "low"}}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "low"
    assert "reasoning_effort" not in payload["chat_template_kwargs"]
    payload = {"reasoning_effort": "high", "thinking_token_budget": 128,
               "reasoning_budget": 0, "chat_template_kwargs": {"reasoning_effort": "low"}}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "high"
    assert payload["thinking_token_budget"] == 0
    assert "reasoning_effort" not in payload["chat_template_kwargs"]


def test_explicit_budget_argument_only_applies_to_verified_glm(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"enable_thinking": False}}
    controls.apply_controls(payload, "http://configured/v1", "qwen3.8", reasoning_budget=0)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: False)
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4", reasoning_budget=128)
    assert payload == {"chat_template_kwargs": {"enable_thinking": False}}
