"""Tests for src/token_calibration.py and its hooks in src/llm_trace.py /
routes/token_calibration_routes.py.
"""
import asyncio
import json
import os

import pytest

from src import token_calibration as tc


@pytest.fixture(autouse=True)
def isolated_calibration_file(tmp_path, monkeypatch):
    """Point token_calibration at a scratch file and reset its cache."""
    path = os.path.join(str(tmp_path), "token_calibration.json")
    monkeypatch.setattr(tc, "_file_path", lambda: path)
    tc._reset_for_tests()
    yield path
    tc._reset_for_tests()


# ---------------------------------------------------------------------------
# factor(): EMA, clamp, min-samples
# ---------------------------------------------------------------------------

def test_factor_is_1_0_below_min_samples():
    with tc._STATE_LOCK:
        tc._load_locked()["qwen2.5"] = {"ratio_ema": 1.6, "samples": 2, "last_update": 0}
    assert tc.factor("qwen2.5") == 1.0


def test_factor_uses_ema_once_min_samples_reached():
    with tc._STATE_LOCK:
        tc._load_locked()["qwen2.5"] = {"ratio_ema": 1.2, "samples": 3, "last_update": 0}
    assert tc.factor("qwen2.5") == 1.2
    assert tc.factor("QWEN2.5") == 1.2  # normalized key is case-insensitive


def test_factor_clamped_to_0_6_1_8():
    with tc._STATE_LOCK:
        state = tc._load_locked()
        state["huge"] = {"ratio_ema": 5.0, "samples": 10, "last_update": 0}
        state["tiny"] = {"ratio_ema": 0.05, "samples": 10, "last_update": 0}
    assert tc.factor("huge") == 1.8
    assert tc.factor("tiny") == 0.6


def test_factor_unknown_model_is_1_0():
    assert tc.factor("never-seen-model") == 1.0
    assert tc.factor("") == 1.0
    assert tc.factor(None) == 1.0


def test_ema_update_math():
    """observe() twice and check the EMA follows alpha=0.2 exactly."""
    messages = [{"role": "user", "content": "x" * 1000}]  # estimate ~304 tokens
    est = tc.estimate_tokens_for(messages, "no-calibration-yet")
    assert est > tc.MIN_ESTIMATE_TOKENS

    # First sample: ratio = 2.0 -> ratio_ema initialized to 2.0 (clamped later on read)
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": est * 2})
    entry = tc._load_locked()["m1"]
    assert entry["samples"] == 1
    assert entry["ratio_ema"] == pytest.approx(2.0, rel=1e-6)

    # Second sample: ratio = 1.0 -> ema = 2.0*0.8 + 1.0*0.2 = 1.8
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": est * 1})
    entry = tc._load_locked()["m1"]
    assert entry["samples"] == 2
    assert entry["ratio_ema"] == pytest.approx(1.8, rel=1e-6)


# ---------------------------------------------------------------------------
# sanity filters
# ---------------------------------------------------------------------------

def test_observe_ignores_small_estimate():
    messages = [{"role": "user", "content": "hi"}]  # estimate << 200 tokens
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": 5000})
    assert "m1" not in tc._load_locked()


def test_observe_ignores_out_of_band_ratio():
    messages = [{"role": "user", "content": "x" * 2000}]  # estimate ~604 tokens
    est = tc.estimate_tokens_for(messages, "m1")
    # ratio way above RATIO_MAX (3.0)
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": est * 10})
    assert "m1" not in tc._load_locked()
    # ratio way below RATIO_MIN (0.3)
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": est * 0.05})
    assert "m1" not in tc._load_locked()


def test_observe_ignores_ollama_cache_hit_undercount():
    messages = [{"role": "user", "content": "x" * 3000}]  # estimate ~904 tokens
    est = tc.estimate_tokens_for(messages, "qwen-local")
    # prompt_eval_count far below 0.5 * estimate -> looks like a KV-cache hit,
    # must be ignored rather than treated as "estimate is way too high".
    tc.observe(
        model="qwen-local",
        request_messages=messages,
        usage={"input_tokens": int(est * 0.1)},
        provider_kind="ollama",
    )
    assert "qwen-local" not in tc._load_locked()

    # But the SAME small value from a non-ollama provider (a genuine total)
    # is accepted if the ratio itself is in-band... here it is not (0.1 <
    # RATIO_MIN 0.3), so use a value that clears RATIO_MIN instead.
    tc.observe(
        model="qwen-local",
        request_messages=messages,
        usage={"input_tokens": int(est * 0.5)},
        provider_kind="ollama",
    )
    assert "qwen-local" in tc._load_locked()  # 0.5 * estimate clears the floor


def test_observe_accepts_ollama_sample_above_floor():
    messages = [{"role": "user", "content": "x" * 3000}]
    est = tc.estimate_tokens_for(messages, "qwen-local2")
    tc.observe(
        model="qwen-local2",
        request_messages=messages,
        usage={"input_tokens": int(est * 0.9)},
        provider_kind="ollama",
    )
    assert "qwen-local2" in tc._load_locked()


def test_observe_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(tc, "calibration_enabled", lambda: False)
    messages = [{"role": "user", "content": "x" * 2000}]
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": 5000})
    assert "m1" not in tc._load_locked()


def test_observe_tools_added_to_estimate_side():
    """A big tools schema counts toward the estimate side of the ratio."""
    messages = [{"role": "user", "content": "hi there, short message"}]
    tools = [{"type": "function", "function": {"name": "x", "parameters": {"p": "y" * 2000}}}]
    est_with_tools = tc.estimate_tokens(messages) + tc._tools_size_tokens(tools)
    assert est_with_tools >= tc.MIN_ESTIMATE_TOKENS
    tc.observe(model="tooled", request_messages=messages, tools=tools, usage={"input_tokens": est_with_tools})
    assert "tooled" in tc._load_locked()
    assert tc._load_locked()["tooled"]["ratio_ema"] == pytest.approx(1.0, rel=1e-2)


def test_observe_ignores_malformed_usage():
    messages = [{"role": "user", "content": "x" * 2000}]
    tc.observe(model="m1", request_messages=messages, usage=None)
    tc.observe(model="m1", request_messages=messages, usage={})
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": "not-a-number"})
    tc.observe(model="m1", request_messages=messages, usage={"input_tokens": -5})
    assert "m1" not in tc._load_locked()


# ---------------------------------------------------------------------------
# estimate_tokens_for()
# ---------------------------------------------------------------------------

def test_estimate_tokens_for_applies_factor():
    messages = [{"role": "user", "content": "x" * 1000}]
    with tc._STATE_LOCK:
        tc._load_locked()["calibrated-model"] = {"ratio_ema": 1.5, "samples": 5, "last_update": 0}
    base = tc.estimate_tokens(messages)
    calibrated = tc.estimate_tokens_for(messages, "calibrated-model")
    assert calibrated == round(base * 1.5)
    assert tc.estimate_tokens_for(messages, "unknown-model") == base


# ---------------------------------------------------------------------------
# persistence round trip
# ---------------------------------------------------------------------------

def test_persistence_round_trip(isolated_calibration_file):
    messages = [{"role": "user", "content": "x" * 2000}]
    est = tc.estimate_tokens_for(messages, "persist-me")
    tc.observe(model="persist-me", request_messages=messages, usage={"input_tokens": est})
    tc.flush_for_tests()
    assert os.path.isfile(isolated_calibration_file)
    with open(isolated_calibration_file) as f:
        raw = json.load(f)
    assert "persist-me" in raw
    assert raw["persist-me"]["samples"] == 1

    # A fresh in-process cache (simulating a new process) reloads it.
    tc._reset_for_tests()
    info = tc.get_info("persist-me")
    assert info["samples"] == 1


# ---------------------------------------------------------------------------
# observe() fed via a fake stream_llm usage event (through llm_trace)
# ---------------------------------------------------------------------------

class _FakeStreamClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, json=None, headers=None, timeout=None):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def status_code(self):
        return 200

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


def test_observe_via_stream_llm_usage_event(isolated_calibration_file, monkeypatch, tmp_path):
    from src import llm_core, llm_trace

    traces_dir = str(tmp_path / "traces")
    monkeypatch.setattr(llm_trace, "_traces_dir", lambda: (os.makedirs(traces_dir, exist_ok=True) or traces_dir))
    monkeypatch.setattr(llm_trace, "tracing_enabled", lambda: True)
    monkeypatch.setattr(tc, "calibration_enabled", lambda: True)

    big_content = "y" * 3000  # estimate ~904 tokens, clears MIN_ESTIMATE_TOKENS
    usage_tokens = 1800  # deliberately different from the raw estimate
    lines = [
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":%d,"completion_tokens":3}}' % usage_tokens,
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
            "gpt-calib-test",
            [{"role": "user", "content": big_content}],
            session_id="calib-sess-1",
        ):
            out.append(chunk)
        return out

    asyncio.run(run())
    llm_trace.flush_for_tests()
    tc.flush_for_tests()

    entry = tc._load_locked().get("gpt-calib-test")
    assert entry is not None
    assert entry["samples"] == 1
    est = tc.estimate_tokens([{"role": "user", "content": big_content}])
    assert entry["ratio_ema"] == pytest.approx(usage_tokens / est, rel=1e-3)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_api_token_calibration(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.token_calibration_routes import setup_token_calibration_routes
    from core import middleware

    monkeypatch.setattr(middleware, "require_admin", lambda request: None)
    import routes.token_calibration_routes as route_mod
    monkeypatch.setattr(route_mod, "require_admin", lambda request: None)

    with tc._STATE_LOCK:
        tc._load_locked()["api-model"] = {"ratio_ema": 1.3, "samples": 4, "last_update": 123.0}

    app = FastAPI()
    app.include_router(setup_token_calibration_routes())
    client = TestClient(app)
    resp = client.get("/api/token-calibration")
    assert resp.status_code == 200
    body = resp.json()
    assert body["models"]["api-model"]["factor"] == pytest.approx(1.3, rel=1e-6)
    assert body["models"]["api-model"]["samples"] == 4
