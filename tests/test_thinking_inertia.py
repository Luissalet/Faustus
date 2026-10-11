# -*- coding: utf-8 -*-
"""Thinking inertia (#275): a fast turn must switch reasoning off in the engine,
the reasoning that still leaks is measured, and a leaky model is capped."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from src import llm_core, thinking_inertia as ti
from src import think_calibration as tc


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(ti, "_path_override", str(tmp_path / "inertia.json"))
    yield


# ----------------------------------------------------------------- measuring

def test_measure_prefers_the_engine_count_and_falls_back_to_an_estimate():
    assert ti.measure("", None) == {"tokens": 0, "chars": 0, "source": "estimate"}
    est = ti.measure("a" * 400)
    assert est["tokens"] == 100 and est["chars"] == 400 and est["source"] == "estimate"
    eng = ti.measure("a" * 400, {"reasoning_tokens": 7})
    assert eng == {"tokens": 7, "chars": 400, "source": "engine"}
    assert ti.measure("abcd", {"reasoning_tokens": "bad"})["source"] == "estimate"


def test_only_fast_turns_are_stamped():
    assert ti.leak_stamp({"mode": "think"}, "x" * 4000, None, "m") == {}
    assert ti.cap_for("m") is None
    stamp = ti.leak_stamp({"mode": "fast"}, "", None, "m")
    assert stamp == {"thinking_leak_tokens": 0, "thinking_leak_chars": 0}


# ------------------------------------------------------------------ the cap

def test_a_leak_beyond_the_threshold_caps_the_next_fast_turn_then_clears():
    assert ti.cap_for("Qwen") is None
    ti.record_fast_turn("Qwen", 10)
    assert ti.cap_for("qwen") is None
    state = ti.record_fast_turn("qwen", ti.DEFAULT_THRESHOLD_TOKENS + 1)
    assert state["capped"] and state["cap"] == ti.DEFAULT_CAP_TOKENS
    assert ti.cap_for("QWEN") == ti.DEFAULT_CAP_TOKENS
    assert "qwen" in ti.caps()
    # quiet turns push the leak out of the window
    for _ in range(ti.RECENT_WINDOW):
        ti.record_fast_turn("qwen", 0)
    assert ti.cap_for("qwen") is None and ti.caps() == {}


def test_the_threshold_and_cap_are_settings(monkeypatch):
    from src import settings
    monkeypatch.setattr(settings, "get_setting",
                        lambda k, d=None: {"think_leak_threshold_tokens": 10, "think_leak_cap_tokens": 50}.get(k, d))
    ti.record_fast_turn("m", 11)
    assert ti.cap_for("m") == 50


def test_leak_stamp_flags_a_leaky_model():
    out = ti.leak_stamp({"mode": "fast"}, "x" * 2000, None, "m")
    assert out["thinking_leak_tokens"] == 500 and out["thinking_leaky"] is True


def test_summary_reports_leak_rate_per_model_and_the_cap():
    ti.record_fast_turn("big", 300)
    recs = [
        {"mode": "fast", "model": "big", "thinking_leak_tokens": 300},
        {"mode": "fast", "model": "big", "thinking_leak_tokens": 0, "thinking_cap": 128},
        {"mode": "fast", "model": "big", "thinking_leak_tokens": None},
        {"mode": "fast", "model": "calm", "thinking_leak_tokens": 3},
        {"mode": "think", "model": "big", "thinking_leak_tokens": 9999},
    ]
    s = ti.summarize(recs)
    big = s["by_model"]["big"]
    assert (big["fast_turns"], big["measured"], big["leaked"], big["leak_rate"]) == (3, 2, 1, 0.5)
    assert big["max_leak_tokens"] == 300 and big["capped_turns"] == 1 and big["avg_leak_tokens"] == 150.0
    assert s["by_model"]["calm"]["leak_rate"] == 0.0
    assert s["total"]["fast_turns"] == 4
    assert s["capped_models"]["big"]["cap"] == ti.DEFAULT_CAP_TOKENS
    flag = s["flags"][0]
    assert flag["key"] == "big" and flag["capped_now"] and str(ti.DEFAULT_CAP_TOKENS) in flag["hint"]


def test_calibration_carries_model_leak_and_cap_in_its_report():
    meta = {"model": "big", "think_mode": {"mode": "fast", "source": "rule", "reasons": ["small_talk"],
                                           "thinking_leak_tokens": 200, "thinking_cap": 128}}
    rows = [{"session_id": "s", "role": "user", "content": "hola"},
            {"session_id": "s", "role": "assistant", "content": "hola", "metadata": json.dumps(meta)}]
    recs = tc.outcomes(rows)
    assert recs[0]["model"] == "big" and recs[0]["thinking_leak_tokens"] == 200 and recs[0]["thinking_cap"] == 128
    out = tc.summarize(recs)
    assert out["thinking_leak"]["by_model"]["big"]["leaked"] == 1


# ------------------------------------------------------ the saved turn record

def _save(turn, thinking_text, metrics=None):
    from routes.chat_helpers import save_assistant_response
    added = []
    sess = SimpleNamespace(model="big", owner="o", history=[], add_message=lambda m: added.append(m),
                           _turn_think_mode=turn)
    manager = SimpleNamespace(save_session=lambda *a, **k: None, update_session=lambda *a, **k: None,
                              save_sessions=lambda *a, **k: None)
    text = f"<think>{thinking_text}</think>Hecho." if thinking_text else "Hecho."
    try:
        save_assistant_response(sess, manager, "sid", text, dict(metrics or {"model": "big"}))
    except Exception:
        pass
    return (added[-1].metadata or {}).get("think_mode")


def test_a_fast_turn_that_still_reasoned_records_the_leak():
    tm = _save({"mode": "fast", "source": "rule"}, "pensando " * 100)
    assert tm["thinking_leak_tokens"] > 100 and tm["thinking_leak_chars"] == 900 - 1
    assert ti.cap_for("big") == ti.DEFAULT_CAP_TOKENS


def test_a_clean_fast_turn_records_zero_and_a_think_turn_records_nothing():
    assert _save({"mode": "fast", "source": "rule"}, "")["thinking_leak_tokens"] == 0
    assert "thinking_leak_tokens" not in _save({"mode": "think", "source": "rule"}, "razono " * 50)


# --------------------------------------------- the engine switch, per provider

class _Resp:
    status_code = 200

    async def aiter_lines(self):
        yield json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _Ctx:
    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *a):
        return False


class _Client:
    def __init__(self):
        self.payload = {}

    def stream(self, method, url, **kw):
        self.payload = kw.get("json") or {}
        return _Ctx()


def _sent(monkeypatch, url, model, overrides, tools=None):
    client = _Client()
    monkeypatch.setattr(llm_core, "_ollama_model_caps", lambda u, m: None)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda u, m: 32768)

    async def run():
        return [c async for c in llm_core.stream_llm(url, model, [{"role": "user", "content": "hola"}],
                                                     tools=tools, gen_overrides=overrides)]
    asyncio.run(run())
    return client.payload


def test_self_hosted_engine_gets_enable_thinking_false(monkeypatch):
    p = _sent(monkeypatch, "http://192.168.1.50:8081/v1", "qwen3.8-27b", {"think": False})
    assert p["chat_template_kwargs"]["enable_thinking"] is False
    assert "reasoning_budget" not in p and "thinking_budget_tokens" not in p


def test_self_hosted_engine_gets_the_cap_as_a_budget_on_a_fast_turn(monkeypatch):
    p = _sent(monkeypatch, "http://192.168.1.50:8081/v1", "qwen3.8-27b", {"think": False, "reasoning_budget": 128})
    assert p["chat_template_kwargs"]["enable_thinking"] is False
    assert p["reasoning_budget"] == 128 and p["thinking_budget_tokens"] == 128


def test_a_think_turn_keeps_its_own_budget(monkeypatch):
    p = _sent(monkeypatch, "http://192.168.1.50:8081/v1", "qwen3.8-27b", {"think": True, "reasoning_budget": 4096})
    assert p["chat_template_kwargs"]["enable_thinking"] is True and p["reasoning_budget"] == 4096


def test_ollama_v1_gets_think_false(monkeypatch):
    p = _sent(monkeypatch, "http://127.0.0.1:11434/v1", "qwen3:8b", {"think": False})
    assert p["think"] is False


def test_a_loopback_llama_server_gets_the_engine_switch_and_the_cap_too(monkeypatch):
    p = _sent(monkeypatch, "http://127.0.0.1:9811/v1", "qwen3-leaky", {"think": False, "reasoning_budget": 128})
    assert p["think"] is False and p["chat_template_kwargs"]["enable_thinking"] is False
    assert p["reasoning_budget"] == 128 and p["thinking_budget_tokens"] == 128


def test_mistral_fast_turn_asks_for_no_reasoning(monkeypatch):
    p = _sent(monkeypatch, "https://api.mistral.ai/v1", "magistral-medium-latest", {"think": False})
    assert p["reasoning_effort"] == "none"


def test_openrouter_fast_turn_says_no_reasoning(monkeypatch):
    p = _sent(monkeypatch, "https://openrouter.ai/api/v1", "qwen/qwen3-235b", {"think": False})
    assert p["reasoning"] == {"effort": "none"}


def test_openai_gpt5_fast_turn_uses_the_lowest_effort_it_accepts(monkeypatch):
    p = _sent(monkeypatch, "https://api.openai.com/v1", "gpt-5-mini", {"think": False})
    assert p["reasoning_effort"] == "minimal"
    p = _sent(monkeypatch, "https://api.openai.com/v1", "gpt-5.1", {"think": False})
    assert p["reasoning_effort"] == "none"


def test_deepseek_fast_turn_disables_thinking_but_the_reasoner_is_left_alone(monkeypatch):
    p = _sent(monkeypatch, "https://api.deepseek.com/v1", "deepseek-v4", {"think": False})
    assert p["thinking"] == {"type": "disabled"}
    p = _sent(monkeypatch, "https://api.deepseek.com/v1", "deepseek-reasoner", {"think": False})
    assert "thinking" not in p


def test_a_turn_that_did_not_ask_for_fast_is_untouched(monkeypatch):
    p = _sent(monkeypatch, "https://api.mistral.ai/v1", "magistral-medium-latest", None)
    assert p["reasoning_effort"] != "none"
    p = _sent(monkeypatch, "https://openrouter.ai/api/v1", "qwen/qwen3-235b", None)
    assert "reasoning" not in p


def test_switch_helper_labels_and_anthropic_sends_nothing():
    payload = {"model": "m"}
    assert llm_core._apply_fast_engine_switch(payload, "https://api.anthropic.com", "claude-sonnet-5",
                                              "anthropic", {"think": False}) == []
    assert "thinking" not in payload
    assert llm_core._apply_fast_engine_switch({}, "http://x/v1", "m", "openai", {"think": True}) == []
    assert llm_core._apply_fast_engine_switch({}, "http://127.0.0.1:11434", "qwen3", "ollama",
                                              {"think": False}) == ["think"]


# ------------------------------------------------- the route applies the cap

def test_route_hands_a_leaky_model_a_capped_fast_turn():
    from routes.chat_routes import _resolve_think_mode
    kw = dict(model="qwen3.8-27b", endpoint_url="http://127.0.0.1:8081/v1", chat_mode="chat", workspace=None)
    ov, ev = _resolve_think_mode("fast", {}, "hola", **kw)
    assert ev["mode"] == "fast" and "reasoning_budget" not in ov and "thinking_cap" not in ev
    ti.record_fast_turn("qwen3.8-27b", 500)
    ov, ev = _resolve_think_mode("fast", {}, "hola", **kw)
    assert ov["think"] is False and ov["reasoning_budget"] == ti.DEFAULT_CAP_TOKENS
    assert ev["thinking_cap"] == ti.DEFAULT_CAP_TOKENS
    ov, ev = _resolve_think_mode("think", {}, "hola", **kw)
    assert ov["reasoning_budget"] != ti.DEFAULT_CAP_TOKENS and "thinking_cap" not in ev
