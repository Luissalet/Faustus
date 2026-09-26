"""A non-streaming call that asks for reasoning gets it, in each provider's
words; a 400 about reasoning steps down instead of failing the call."""
import asyncio

import src.llm_core as llm_core


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self.headers = {}
        self.text = text
        self._body = body or {"choices": [{"message": {"content": "ok"}}]}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


def _capture(monkeypatch, responses):
    sent = []

    async def fake_post(client, url, headers, **kw):
        sent.append(dict(kw.get("json") or {}))
        return responses.pop(0) if responses else _Resp()

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda key: None)
    monkeypatch.setattr(llm_core, "_set_cached_response", lambda *a, **k: None)
    return sent


def _call(url, model, **kw):
    return asyncio.run(llm_core.llm_call_async(url, model, [{"role": "user", "content": "plan the research"}],
                                               max_tokens=1000, **kw))


def test_llama_server_thinks_with_budget_and_room(monkeypatch):
    sent = _capture(monkeypatch, [])
    _call("http://127.0.0.1:8081/v1", "qwen3.8-27b-q8-llamacpp",
          gen_overrides={"think": True, "reasoning_effort": "max", "reasoning_budget": 16384})
    p = sent[0]
    assert p["chat_template_kwargs"]["enable_thinking"] is True
    assert p.get("thinking_budget_tokens") == 16384
    assert p["max_tokens"] == 1000 + 16384


def test_helper_calls_still_do_not_think(monkeypatch):
    sent = _capture(monkeypatch, [])
    _call("http://127.0.0.1:8081/v1", "qwen3.8-27b-q8-llamacpp")
    assert sent[0]["chat_template_kwargs"]["enable_thinking"] is False
    assert sent[0]["max_tokens"] == 1000


def test_claude_gets_adaptive_thinking_and_effort(monkeypatch):
    body = {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "plan"}]}
    sent = _capture(monkeypatch, [_Resp(body=body)])
    out = _call("https://api.anthropic.com/v1", "claude-opus-5-5",
                headers={"x-api-key": "test"},
                gen_overrides={"think": True, "reasoning_effort": "max", "reasoning_budget": 16384})
    p = sent[0]
    assert p["thinking"] == {"type": "adaptive"} and p["output_config"] == {"effort": "max"}
    assert "temperature" not in p and out == "plan"


def test_a_refused_top_effort_steps_down_then_drops(monkeypatch):
    sent = _capture(monkeypatch, [
        _Resp(400, text='{"error": "Unsupported value: max for reasoning_effort"}'),
        _Resp(400, text='{"error": "reasoning_effort is not supported with this model"}'),
        _Resp(),
    ])
    out = _call("https://api.openai.com/v1", "gpt-4.1",
                headers={"Authorization": "Bearer test"},
                gen_overrides={"think": True, "reasoning_effort": "max"})
    assert out == "ok"
    assert [p.get("reasoning_effort") for p in sent] == ["max", "high", None]
