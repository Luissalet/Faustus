"""A managed engine that dies in the middle of a local model call is started
again and the call is tried once more (`src.engine_swap.
recover_after_connect_failure`, hooked into `src.llm_core`'s connect-failure
paths once their own retries are spent)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import src.engine_swap as engine_swap
import src.llm_core as llm_core

pytestmark = pytest.mark.asyncio


async def test_recovery_starts_the_engine_again(monkeypatch):
    monkeypatch.setattr(engine_swap, "engine_for_url", lambda u: {"id": "eng-1", "port": 8081})
    monkeypatch.setattr(engine_swap, "_settings", lambda: {"autostart": True, "autostart_timeout_s": 5.0,
                                                          "idle_ttl_minutes": 0.0, "idle_check_s": 30.0})
    calls = []

    async def fake_ready(url, timeout_s=None):
        calls.append(url)
        return {"action": "started", "engine_id": "eng-1"}
    monkeypatch.setattr(engine_swap, "ensure_ready", fake_ready)
    assert await engine_swap.recover_after_connect_failure("http://127.0.0.1:8081/v1") is True
    assert calls == ["http://127.0.0.1:8081/v1"]


async def test_recovery_reports_failure_and_never_raises(monkeypatch):
    monkeypatch.setattr(engine_swap, "engine_for_url", lambda u: {"id": "eng-1", "port": 8081})
    monkeypatch.setattr(engine_swap, "_settings", lambda: {"autostart": True, "autostart_timeout_s": 5.0,
                                                          "idle_ttl_minutes": 0.0, "idle_check_s": 30.0})

    async def failing(url, timeout_s=None):
        return {"action": "failed", "error": "port busy"}
    monkeypatch.setattr(engine_swap, "ensure_ready", failing)
    assert await engine_swap.recover_after_connect_failure("http://127.0.0.1:8081/v1") is False

    async def boom(url, timeout_s=None):
        raise RuntimeError("x")
    monkeypatch.setattr(engine_swap, "ensure_ready", boom)
    assert await engine_swap.recover_after_connect_failure("http://127.0.0.1:8081/v1") is False


async def test_no_managed_engine_or_autostart_off_means_no_recovery(monkeypatch):
    monkeypatch.setattr(engine_swap, "engine_for_url", lambda u: None)
    assert engine_swap.restartable_engine_for_url("http://127.0.0.1:9999/v1") is None
    assert await engine_swap.recover_after_connect_failure("http://127.0.0.1:9999/v1") is False
    monkeypatch.setattr(engine_swap, "engine_for_url", lambda u: {"id": "eng-1"})
    monkeypatch.setattr(engine_swap, "_settings", lambda: {"autostart": False, "autostart_timeout_s": 5.0,
                                                          "idle_ttl_minutes": 0.0, "idle_check_s": 30.0})
    assert engine_swap.restartable_engine_for_url("http://127.0.0.1:8081/v1") is None


# ── llm_core wiring: retries once after a successful engine-recovery ──

class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self.headers = {}
        self.text = ""

    def json(self):
        return {"model": "test-model", "choices": [{"message": {"content": "ok"}}]}


def _fast_sleep(monkeypatch):
    async def _sleep(seconds):
        return None
    monkeypatch.setattr(llm_core.asyncio, "sleep", _sleep)


async def test_non_streaming_call_retries_once_after_engine_recovery(monkeypatch):
    """`llm_call_async` against a local llama.cpp URL: every ordinary retry
    connect-fails, but once they are exhausted and the engine is reported
    managed, a successful `wait_for_recovery` earns exactly one more
    physical attempt, which succeeds."""
    url = "http://127.0.0.1:8081/v1/chat/completions"
    calls = []

    async def fake_post(client, target_url, headers, **kwargs):
        calls.append(target_url)
        if len(calls) <= 2:
            raise httpx.ConnectError("connection refused")
        return _Response(200)

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    _fast_sleep(monkeypatch)
    llm_core._response_cache.clear()

    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda u: {"id": "eng-1"})

    waited = {"n": 0}

    async def fake_wait(u, timeout_s=None):
        waited["n"] += 1
        return True

    monkeypatch.setattr(engine_swap, "recover_after_connect_failure", fake_wait)

    result = await llm_core.llm_call_async(
        url, "test-model", [{"role": "user", "content": "hi"}], max_retries=2,
    )
    assert result == "ok"
    assert waited["n"] == 1
    assert len(calls) == 3  # 2 ordinary attempts (max_retries) + 1 earned by the wait


async def test_non_streaming_call_gives_up_when_recovery_wait_fails(monkeypatch):
    url = "http://127.0.0.1:8081/v1/chat/completions"

    async def fake_post(client, target_url, headers, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    _fast_sleep(monkeypatch)
    llm_core._response_cache.clear()

    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda u: {"id": "eng-1"})

    async def fake_wait(u, timeout_s=None):
        return False

    monkeypatch.setattr(engine_swap, "recover_after_connect_failure", fake_wait)

    with pytest.raises(llm_core.HTTPException) as exc_info:
        await llm_core.llm_call_async(
            url, "test-model", [{"role": "user", "content": "hi"}], max_retries=2,
        )
    assert exc_info.value.status_code == 503


async def test_non_streaming_call_unmanaged_url_fails_normally_without_waiting(monkeypatch):
    url = "https://api.example-unmanaged.test/v1/chat/completions"

    async def fake_post(client, target_url, headers, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    _fast_sleep(monkeypatch)
    llm_core._response_cache.clear()

    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda u: None)

    wait_calls = {"n": 0}

    async def fake_wait(u, timeout_s=None):
        wait_calls["n"] += 1
        return True

    monkeypatch.setattr(engine_swap, "recover_after_connect_failure", fake_wait)

    with pytest.raises(llm_core.HTTPException) as exc_info:
        await llm_core.llm_call_async(
            url, "test-model", [{"role": "user", "content": "hi"}], max_retries=2,
        )
    assert exc_info.value.status_code == 503
    assert wait_calls["n"] == 0  # never even asked to wait for a non-managed URL


# ── streaming wiring: the generic OpenAI-compatible branch ──────────────────

class _Resp:
    def __init__(self, lines=None, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._lines = lines or []

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"upstream error body"


class _StreamCtx:
    def __init__(self, resp=None, raise_on_enter=None):
        self._resp = resp
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter is not None:
            raise self._raise_on_enter
        return self._resp

    async def __aexit__(self, *args):
        return False


class _SeqClient:
    def __init__(self, contexts):
        self._contexts = list(contexts)
        self.calls = 0

    def stream(self, method, url, **kwargs):
        ctx = self._contexts[self.calls]
        self.calls += 1
        return ctx


def _events(chunks):
    import json
    out = []
    for chunk in chunks:
        for raw_line in chunk.split("\n"):
            raw_line = raw_line.strip()
            if raw_line.startswith("data: ") and raw_line[6:] != "[DONE]":
                try:
                    out.append(json.loads(raw_line[6:]))
                except ValueError:
                    pass
    return out


async def test_streaming_call_retries_once_after_engine_recovery(monkeypatch):
    url = "http://127.0.0.1:8081/v1/chat/completions"
    contexts = [
        _StreamCtx(raise_on_enter=httpx.ConnectError("connection refused")),
        _StreamCtx(raise_on_enter=httpx.ConnectError("connection refused")),
        _StreamCtx(resp=_Resp(lines=[
            'data: {"choices":[{"delta":{"content":"back"}}]}',
            "data: [DONE]",
        ])),
    ]
    client = _SeqClient(contexts)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    _fast_sleep(monkeypatch)

    monkeypatch.setattr(engine_swap, "restartable_engine_for_url", lambda u: {"id": "eng-1"})
    waited = {"n": 0}

    async def fake_wait(u, timeout_s=None):
        waited["n"] += 1
        return True

    monkeypatch.setattr(engine_swap, "recover_after_connect_failure", fake_wait)

    chunks = [
        chunk async for chunk in llm_core.stream_llm(
            url, "test-model", [{"role": "user", "content": "hi"}], max_retries=2,
        )
    ]
    events = _events(chunks)
    assert "".join(e["delta"] for e in events if "delta" in e) == "back"
    assert client.calls == 3
    assert waited["n"] == 1
