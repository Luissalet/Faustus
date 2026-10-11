"""A turn against a local server that is down pays the refused connect once, not on every ask (OBJ-57)."""
import httpx
import pytest

import src.model_context as mc
from src import llm_core


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    mc.clear_context_caches()
    monkeypatch.setattr(llm_core, "_dead_hosts", {}, raising=False)
    monkeypatch.setattr(mc, "_configured_endpoint_kind", lambda url: None)
    yield
    mc.clear_context_caches()


def _refusing(calls):
    def get(url, timeout=None, **kw):
        calls.append(url)
        raise httpx.ConnectError("[WinError 10061] connection refused")
    return get


def test_llamacpp_down_is_probed_once_per_turn(monkeypatch):
    calls = []
    monkeypatch.setattr(mc.httpx, "get", _refusing(calls))
    url = "http://127.0.0.1:8081/v1"
    first = mc.get_context_length_known(url, "qwen3-8b")
    for _ in range(5):
        assert mc.get_context_length_known(url, "qwen3-8b") == first
    assert len(calls) <= 2, calls  # /slots once, then /models skipped by the cooldown
    assert any(c.endswith("/slots") for c in calls)


def test_slots_failure_feeds_the_shared_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(mc.httpx, "get", _refusing(calls))
    mc.get_context_length_known("http://127.0.0.1:8082/v1", "m")
    assert llm_core._is_host_dead("http://127.0.0.1:8082/v1/models")
    # A different model on the same dead host skips the network entirely.
    calls.clear()
    mc.get_context_length_known("http://127.0.0.1:8082/v1", "other")
    assert calls == []


def test_ollama_ps_honours_the_cooldown(monkeypatch):
    calls = []
    monkeypatch.setattr(mc.httpx, "get", _refusing(calls))
    monkeypatch.setattr(mc, "_saved_num_ctx", lambda *a: None)
    mc.get_context_length_known("http://127.0.0.1:11434/v1", "qwen3:8b")
    assert any(c.endswith("/api/ps") for c in calls)
    mc._local_context_recent.clear()
    calls.clear()
    mc.get_context_length_known("http://127.0.0.1:11434/v1", "qwen3:8b")
    assert calls == []


def test_local_answer_is_asked_again_after_the_ttl(monkeypatch):
    windows = iter([8192, 32768])

    class R:
        is_success = True

        def __init__(self, n):
            self.n = n

        def json(self):
            return [{"n_ctx": self.n}]

    calls = []

    def get(url, timeout=None, **kw):
        calls.append(url)
        return R(next(windows))

    monkeypatch.setattr(mc.httpx, "get", get)
    url = "http://127.0.0.1:8083/v1"
    assert mc.get_context_length(url, "m") == 8192
    assert mc.get_context_length(url, "m") == 8192  # same turn: remembered
    assert len(calls) == 1
    key = (url, "m")
    ctx, known, ts = mc._local_context_recent[key]
    mc._local_context_recent[key] = (ctx, known, ts - mc.LOCAL_CONTEXT_TTL_S - 1)
    assert mc.get_context_length(url, "m") == 32768  # restarted with another window
