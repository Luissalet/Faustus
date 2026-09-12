"""INF-03 Lote A: `engine_timings` riding the `usage` SSE event.

Ollama's `done` message and llama.cpp's `timings` block both carry a
per-request phase breakdown (load/prefill/generation in ns or ms) that was
being computed into two tok/s ratios and then thrown away. These tests pin
the wire shape `engine_timings` now takes on each backend, and that a
backend which reports neither (a generic OpenAI-compatible cloud provider)
never gets the key at all — absent, never a fabricated null block.
"""
import asyncio
import json

from src import llm_core


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


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _drive(monkeypatch, url, lines, model="test-model"):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda u, m: 32768)

    async def run():
        events = []
        async for chunk in llm_core.stream_llm(url, model, [{"role": "user", "content": "hi"}]):
            for ln in chunk.split("\n"):
                ln = ln.strip()
                if ln.startswith("data: ") and ln[6:] != "[DONE]":
                    try:
                        events.append(json.loads(ln[6:]))
                    except ValueError:
                        pass
        return events

    return asyncio.run(run())


def _usage_event(events):
    for e in events:
        if e.get("type") == "usage":
            return e["data"]
    raise AssertionError(f"no usage event in {events!r}")


def test_ollama_native_usage_carries_engine_timings_in_ms(monkeypatch):
    lines = [
        json.dumps({"model": "llama3", "message": {"content": "hi"}, "done": False}),
        json.dumps({
            "model": "llama3", "done": True, "done_reason": "stop",
            "message": {"content": ""},
            "prompt_eval_count": 10, "prompt_eval_duration": 200_000_000,
            "eval_count": 20, "eval_duration": 400_000_000,
            "load_duration": 50_000_000, "total_duration": 700_000_000,
        }),
    ]
    events = _drive(monkeypatch, "http://localhost:11434", lines)
    usage = _usage_event(events)
    et = usage["engine_timings"]
    assert et["source"] == "ollama"
    # ns -> ms, exact (no rounding surprises at these round numbers).
    assert et["load_ms"] == 50.0
    assert et["prompt_ms"] == 200.0
    assert et["predicted_ms"] == 400.0
    assert et["total_ms"] == 700.0
    assert et["prompt_n"] == 10
    assert et["predicted_n"] == 20


def test_ollama_native_engine_timings_field_absent_stays_none(monkeypatch):
    # No `load_duration` on this `done` (e.g. the model was already loaded so
    # Ollama reports 0 elsewhere but never omits the key in practice — this
    # pins the "omitted key -> None" contract regardless).
    lines = [
        json.dumps({
            "model": "llama3", "done": True, "done_reason": "stop",
            "message": {"content": ""},
            "prompt_eval_count": 5, "prompt_eval_duration": 100_000_000,
            "eval_count": 8, "eval_duration": 160_000_000,
        }),
    ]
    events = _drive(monkeypatch, "http://localhost:11434", lines)
    et = _usage_event(events)["engine_timings"]
    assert et["load_ms"] is None
    assert et["total_ms"] is None
    assert et["prompt_ms"] == 100.0
    assert et["predicted_ms"] == 160.0


def test_llamacpp_openai_compat_usage_carries_engine_timings_no_load(monkeypatch):
    lines = [
        "data: " + json.dumps({
            "model": "qwen-14b",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30},
            "timings": {
                "prompt_ms": 120.5, "predicted_ms": 900.25,
                "prompt_n": 50, "predicted_n": 30,
                "predicted_per_second": 33.3, "prompt_per_second": 415.0,
            },
        }),
        "data: [DONE]",
    ]
    events = _drive(monkeypatch, "http://localhost:8080/v1/chat/completions", lines)
    usage = _usage_event(events)
    et = usage["engine_timings"]
    assert et["source"] == "llamacpp"
    # llama.cpp never reports load time per request — absent, never 0.
    assert et["load_ms"] is None
    assert et["prompt_ms"] == 120.5
    assert et["predicted_ms"] == 900.25
    assert et["prompt_n"] == 50
    assert et["predicted_n"] == 30
    # The two rates this block already extracted keep working unchanged.
    assert usage["gen_tps"] == 33.3
    assert usage["prefill_tps"] == 415.0


def test_generic_openai_usage_has_no_engine_timings_key_at_all(monkeypatch):
    # No `timings` block (a cloud OpenAI-compatible provider) -> the key is
    # absent from the usage dict entirely, never present as `null`.
    lines = [
        "data: " + json.dumps({
            "model": "gpt-test",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }),
        "data: [DONE]",
    ]
    events = _drive(monkeypatch, "https://api.example-openai-compatible.test/v1", lines)
    usage = _usage_event(events)
    assert "engine_timings" not in usage
