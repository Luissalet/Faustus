"""Tests for src/llm_trace.py and its hooks in src/llm_core.py, plus the
routes/llm_trace_routes.py API.
"""
import asyncio
import json
import os

import pytest

from src import llm_trace


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

def test_redact_strips_known_secret_keys():
    payload = {
        "Authorization": "Bearer sk-abc123",
        "api_key": "secret-value",
        "nested": {"token": "xyz", "safe": "keep-me"},
        "list": [{"password": "hunter2"}, "plain"],
    }
    out = llm_trace.redact(payload)
    assert out["Authorization"] == "[REDACTED]"
    assert out["api_key"] == "[REDACTED]"
    assert out["nested"]["token"] == "[REDACTED]"
    assert out["nested"]["safe"] == "keep-me"
    assert out["list"][0]["password"] == "[REDACTED]"
    assert out["list"][1] == "plain"


def test_redact_strips_inline_bearer_string():
    out = llm_trace.redact({"header_line": "Authorization: Bearer sk-live-999"})
    assert "sk-live-999" not in out["header_line"]
    assert "Bearer [REDACTED]" in out["header_line"]


# ---------------------------------------------------------------------------
# size guard
# ---------------------------------------------------------------------------

def test_guard_messages_under_limit_kept_verbatim(monkeypatch):
    monkeypatch.setattr(llm_trace, "_max_request_chars", lambda: 2_000_000)
    req = {"messages": [{"role": "user", "content": "hi"}]}
    out = llm_trace._guard_messages(req)
    assert out["messages"] == req["messages"]
    assert "messages_omitted" not in out


def test_guard_messages_over_limit_replaced_with_fingerprint(monkeypatch):
    monkeypatch.setattr(llm_trace, "_max_request_chars", lambda: 50)
    big = [{"role": "user", "content": "x" * 500}]
    req = {"messages": big}
    out = llm_trace._guard_messages(req)
    assert out["messages"] is None
    assert "sha256" in out["messages_omitted"]
    assert len(out["messages_omitted"]["sha256"]) == 64


# ---------------------------------------------------------------------------
# record / list / get round trip
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_traces_dir(tmp_path, monkeypatch):
    d = str(tmp_path / "llm_traces")
    os.makedirs(d, exist_ok=True)
    monkeypatch.setattr(llm_trace, "_traces_dir", lambda: (os.makedirs(d, exist_ok=True) or d))
    # fresh seq cache per test
    llm_trace._SEQ_CACHE.clear()
    return d


def test_record_call_disabled_setting_is_noop(isolated_traces_dir, monkeypatch):
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: False)
    llm_trace.record_call(session_id="s1", model="m", request={"messages": []}, response_text="hi")
    llm_trace.flush_for_tests()
    assert llm_trace.list_calls("s1") == []


def test_record_call_no_session_is_noop(isolated_traces_dir):
    llm_trace.record_call(session_id=None, model="m", request={"messages": []}, response_text="hi")
    llm_trace.flush_for_tests()
    assert llm_trace.list_calls("None") == []
    assert os.listdir(isolated_traces_dir) == []


def test_record_call_and_list_and_get(isolated_traces_dir, monkeypatch):
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    llm_trace.record_call(
        session_id="sess-a",
        endpoint_url="https://api.example.com/v1/chat/completions?key=abc",
        model="gpt-test",
        request={"messages": [{"role": "user", "content": "hello"}], "temperature": 0.5,
                 "headers": {"Authorization": "Bearer sk-secret"}},
        response_text="hello back",
        duration_ms=12.3,
        usage={"input_tokens": 3, "output_tokens": 2},
        finish_reason="stop",
    )
    llm_trace.flush_for_tests()

    rows = llm_trace.list_calls("sess-a")
    assert len(rows) == 1
    assert rows[0]["seq"] == 1
    assert rows[0]["model"] == "gpt-test"
    assert rows[0]["response_preview"] == "hello back"
    assert "key=abc" not in (rows[0]["endpoint"] or "")

    full = llm_trace.get_call("sess-a", 1)
    assert full["request"]["headers"]["Authorization"] == "[REDACTED]"
    assert full["usage"]["input_tokens"] == 3
    assert full["finish_reason"] == "stop"

    assert llm_trace.get_call("sess-a", 999) is None


def test_seq_is_monotonic_per_session(isolated_traces_dir, monkeypatch):
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    for i in range(3):
        llm_trace.record_call(session_id="sess-b", model="m", request={"messages": []},
                               response_text=f"r{i}")
    llm_trace.flush_for_tests()
    rows = llm_trace.list_calls("sess-b")
    assert [r["seq"] for r in rows] == [1, 2, 3]


def test_retention_deletes_old_files(isolated_traces_dir, monkeypatch):
    import time
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    monkeypatch.setattr(llm_trace, "_retention_days", lambda: 1)
    llm_trace.record_call(session_id="old-sess", model="m", request={"messages": []}, response_text="r")
    llm_trace.flush_for_tests()
    path = llm_trace._log_path("old-sess")
    assert os.path.isfile(path)
    old_time = time.time() - 2 * 86400
    os.utime(path, (old_time, old_time))
    # force the sweep to run regardless of the normal rate limit
    llm_trace._last_retention_sweep = 0.0
    llm_trace._sweep_retention()
    assert not os.path.isfile(path)


# ---------------------------------------------------------------------------
# StreamAccumulator
# ---------------------------------------------------------------------------

def test_stream_accumulator_collects_text_thinking_tools_usage_finish():
    acc = llm_trace.StreamAccumulator()
    acc.feed('data: {"delta": "Hel"}\n\n')
    acc.feed('data: {"delta": "lo", "thinking": true}\n\n')
    acc.feed('data: {"delta": "lo"}\n\n')
    acc.feed('data: {"type": "tool_calls", "calls": [{"name": "bash", "arguments": "{}"}]}\n\n')
    acc.feed('data: {"type": "usage", "data": {"input_tokens": 5}}\n\n')
    acc.feed('data: {"type": "finish", "finish_reason": "stop"}\n\n')
    assert acc.text == "Hello"
    assert acc.thinking == "lo"
    assert acc.tool_calls == [{"name": "bash", "arguments": "{}"}]
    assert acc.usage == {"input_tokens": 5}
    assert acc.finish_reason == "stop"
    assert acc.error is None


def test_stream_accumulator_captures_error_event():
    acc = llm_trace.StreamAccumulator()
    acc.feed('event: error\ndata: {"error": "boom", "status": 500}\n\n')
    assert acc.error == "boom"


# ---------------------------------------------------------------------------
# llm_core hooks (non-streaming + streaming)
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeStreamClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def test_stream_llm_hook_records_full_call(isolated_traces_dir, monkeypatch):
    from src import llm_core
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    lines = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"delta":{"content":" there"}}]}',
        'data: [DONE]',
    ]
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeStreamClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        out = []
        async for chunk in llm_core.stream_llm(
            "https://api.openai.com/v1/chat/completions",
            "gpt-test",
            [{"role": "user", "content": "hi"}],
            session_id="trace-sess-1",
        ):
            out.append(chunk)
        return out

    asyncio.run(run())
    llm_trace.flush_for_tests()
    rows = llm_trace.list_calls("trace-sess-1")
    assert len(rows) == 1
    assert rows[0]["response_preview"] == "Hi there"
    full = llm_trace.get_call("trace-sess-1", 1)
    assert full["request"]["messages"] == [{"role": "user", "content": "hi"}]


def test_stream_llm_no_session_records_nothing(isolated_traces_dir, monkeypatch):
    from src import llm_core
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    lines = ['data: {"choices":[{"delta":{"content":"Hi"}}]}', 'data: [DONE]']
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeStreamClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        async for _ in llm_core.stream_llm(
            "https://api.openai.com/v1/chat/completions",
            "gpt-test",
            [{"role": "user", "content": "hi"}],
            session_id=None,
        ):
            pass

    asyncio.run(run())
    llm_trace.flush_for_tests()
    assert os.listdir(isolated_traces_dir) == []


def test_llm_call_async_hook_records_nonstreaming(isolated_traces_dir, monkeypatch):
    from src import llm_core

    class _FakeNonStreamResp:
        status_code = 200
        is_success = True

        def json(self):
            return {"choices": [{"message": {"content": "non-stream reply"}}]}

        @property
        def text(self):
            return "ok"

    class _FakeAsyncClient:
        async def post(self, url, **kw):
            return _FakeNonStreamResp()

    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeAsyncClient())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)

    async def run():
        return await llm_core.llm_call_async(
            "https://api.openai.com/v1/chat/completions",
            "gpt-test",
            [{"role": "user", "content": "hello"}],
            session_id="trace-sess-2",
        )

    text = asyncio.run(run())
    assert text == "non-stream reply"
    llm_trace.flush_for_tests()
    rows = llm_trace.list_calls("trace-sess-2")
    assert len(rows) == 1
    assert rows[0]["response_preview"] == "non-stream reply"


# ---------------------------------------------------------------------------
# API: list / get / fork + owner isolation
# ---------------------------------------------------------------------------

def _make_client(monkeypatch):
    """Minimal FastAPI app carrying only the traces router, with auth and
    ownership stubbed so the test exercises routing/logic, not the full auth
    stack (already covered by other suites)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routes.llm_trace_routes as rt

    monkeypatch.setattr(rt, "require_user", lambda request: "alice")
    monkeypatch.setattr(rt, "effective_user", lambda request: "alice")

    def _verify_owner(request, session_id, session_manager=None):
        if session_id == "not-mine":
            from fastapi import HTTPException
            raise HTTPException(404, "not found")
        return None

    monkeypatch.setattr(rt, "_verify_session_owner", _verify_owner)

    app = FastAPI()
    app.include_router(rt.setup_llm_trace_routes())
    return TestClient(app)


def test_api_list_and_get(isolated_traces_dir, monkeypatch):
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    llm_trace.record_call(session_id="api-sess", model="m1", request={"messages": [{"role": "user", "content": "hi"}]},
                           response_text="hello")
    llm_trace.flush_for_tests()

    client = _make_client(monkeypatch)
    r = client.get("/api/llm-traces/api-sess")
    assert r.status_code == 200
    body = r.json()
    assert len(body["calls"]) == 1
    assert body["calls"][0]["model"] == "m1"

    r2 = client.get("/api/llm-traces/api-sess/1")
    assert r2.status_code == 200
    assert r2.json()["response_text"] == "hello"

    r3 = client.get("/api/llm-traces/api-sess/999")
    assert r3.status_code == 404


def test_api_owner_isolation(isolated_traces_dir, monkeypatch):
    client = _make_client(monkeypatch)
    r = client.get("/api/llm-traces/not-mine")
    assert r.status_code == 404


def test_api_fork(isolated_traces_dir, monkeypatch):
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    llm_trace.record_call(
        session_id="fork-sess",
        endpoint_url="https://api.openai.com/v1/chat/completions",
        model="gpt-original",
        request={"messages": [{"role": "user", "content": "hi"}], "temperature": 0.3, "max_tokens": 100},
        response_text="original reply",
    )
    llm_trace.flush_for_tests()

    from src import llm_core as real_llm_core

    async def _fake_stream_llm(url, model, messages, **kwargs):
        assert kwargs.get("session_id") is None  # fork calls must not self-trace
        yield 'data: {"delta": "forked reply"}\n\n'
        yield 'data: [DONE]\n\n'

    def _fake_resolve_endpoint(prefix, fallback_url=None, fallback_model=None, owner=None):
        return "https://api.other.com/v1/chat/completions", fallback_model, {}

    # The route does `from src import llm_core` / `from src.endpoint_resolver
    # import resolve_endpoint` INSIDE the handler (deferred import), so
    # patching the real module attributes here is what the handler's own
    # import picks up at call time.
    monkeypatch.setattr(real_llm_core, "stream_llm", _fake_stream_llm)
    import src.endpoint_resolver as er
    monkeypatch.setattr(er, "resolve_endpoint", _fake_resolve_endpoint)

    client = _make_client(monkeypatch)

    r = client.post("/api/llm-traces/fork-sess/1/fork", json={"model": "gpt-forked"})
    assert r.status_code == 200
    body = r.json()
    assert body["original"]["response_text"] == "original reply"
    assert body["fork"]["model"] == "gpt-forked"
    assert body["fork"]["text"] == "forked reply"


def test_redaction_keeps_token_counters_and_sampling_knobs():
    from src import llm_trace
    out = llm_trace.redact({"max_tokens": 512, "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                            "tokenizer": "x", "api_key": "sk-1", "access_token": "abc", "refresh_token": "d"})
    assert out["max_tokens"] == 512
    assert out["usage"] == {"prompt_tokens": 3, "completion_tokens": 4}
    assert out["tokenizer"] == "x"
    assert out["api_key"] == out["access_token"] == out["refresh_token"] == "[REDACTED]"
