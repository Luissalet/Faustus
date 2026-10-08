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


def test_native_off_and_effort_are_independent(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"chat_template_kwargs": {"enable_thinking": False},
               "reasoning_effort": "high", "reasoning_budget": 4096}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"] == 0
    assert payload["reasoning_effort"] == "high"
    assert "reasoning_budget" not in payload
    payload["chat_template_kwargs"]["enable_thinking"] = True
    payload["reasoning_budget"] = 128
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["thinking_token_budget"] == 128


def test_unsupported_effort_never_silently_reaches_template(monkeypatch):
    monkeypatch.setattr(controls, "vllm_thinking_budget", lambda _: True)
    payload = {"reasoning_effort": "xhigh"}
    controls.apply_controls(payload, "http://configured/v1", "glm-5.3-flash-nvfp4")
    assert payload["reasoning_effort"] == "max"
