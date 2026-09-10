"""A non-streamed call to a local model may take as long as the generation.

llm_call_async's read timeout is the whole answer. 180 s fits a cloud API;
a 27B on consumer cards needs 10-15 minutes for an 8192-token synthesis, and
with the fixed figure every synthesis on Ollama timed out and the round's
findings were dropped (09-09-2026).
"""
import pytest

from src.deep_research import DeepResearcher


def _researcher(url: str) -> DeepResearcher:
    return DeepResearcher(llm_endpoint=url, llm_model="qwen3.8:27b-q4_K_M", max_report_tokens=8192)


def test_local_endpoint_gets_prefill_plus_generation(monkeypatch):
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    r = _researcher("http://127.0.0.1:11434")
    # 120 s prefill + 8192 tokens at 8 tok/s
    assert r._call_budget(8192, 180) == 120 + 1024
    # never below what the caller asked for
    assert r._call_budget(20, 15) == 120 + 2
    assert r._call_budget(0, 600) == 600


def test_remote_endpoint_keeps_the_callers_figure(monkeypatch):
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: False)
    r = _researcher("https://api.openai.com/v1")
    assert r._call_budget(8192, 180) == 180
    assert r._call_budget(128, 60) == 60


def test_speed_setting_scales_the_budget(monkeypatch):
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 32 if key == "research_local_tokens_per_second" else default)
    r = _researcher("http://localhost:11434")
    assert r._call_budget(8192, 180) == 120 + 256


def test_budget_is_capped_at_an_hour(monkeypatch):
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: 0.5 if key == "research_local_tokens_per_second" else default)
    r = _researcher("http://localhost:11434")
    assert r._call_budget(8192, 180) == 3600



def test_the_speed_the_model_has_shown_beats_the_setting(monkeypatch):
    from src import llm_core
    monkeypatch.setattr("src.model_context.is_local_endpoint", lambda url: True)
    monkeypatch.setattr(llm_core, "_LOCAL_SPEED", {})
    r = _researcher("http://127.0.0.1:11434")
    # nothing learned yet: the setting (8 tok/s) applies
    assert r._call_budget(8192, 180) == 120 + 1024
    # 300 tokens in 15 s = 20 tok/s, learned from Ollama's own counters
    llm_core.remember_local_speed("qwen3.8:27b-q4_K_M", 300, 15_000_000_000)
    assert llm_core.local_speed("qwen3.8:27b-q4_K_M") == 20.0
    # used with a third of slack: 8192 / (20 × 0.66)
    assert r._call_budget(8192, 180) == int(120 + 8192 / (20 * 0.66))


def test_tiny_replies_teach_nothing_and_the_average_moves_slowly():
    from src import llm_core
    llm_core._LOCAL_SPEED.clear()
    assert llm_core.remember_local_speed("m", 3, 150_000_000) is None       # "cinco"
    assert llm_core.local_speed("m") is None
    llm_core.remember_local_speed("m", 100, 10_000_000_000)                # 10 tok/s
    llm_core.remember_local_speed("m", 100, 2_000_000_000)                 # one fast call: 50 tok/s
    assert llm_core.local_speed("m") == 22.0                               # 10×0.7 + 50×0.3
    assert llm_core.remember_local_speed("m", None, None) is None
    llm_core._LOCAL_SPEED.clear()
