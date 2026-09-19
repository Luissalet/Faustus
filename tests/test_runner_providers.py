"""src/runner_providers.py — residency probing for a self-hosted OpenAI-
compatible runner (llama.cpp's llama-server), the provider a llama-server
model endpoint needed so it stops being invisible next to Ollama.
"""
from __future__ import annotations

import httpx
import pytest

from src import runner_providers as rp

ROOT = "http://127.0.0.1:8081"


@pytest.fixture(autouse=True)
def clean_cache():
    rp.clear_cache()
    yield
    rp.clear_cache()


def _mock_get(monkeypatch, routes: dict):
    """`routes`: {"/health": (status, json)|None, ...}. `None` means the
    request raises (connection refused)."""
    def fake_get(url, timeout=None, verify=None):
        for path, resp in routes.items():
            if url == ROOT + path:
                if resp is None:
                    raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
                status, payload = resp
                return httpx.Response(status, json=payload, request=httpx.Request("GET", url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)


def test_probe_reads_model_context_and_footprint(monkeypatch, tmp_path):
    gguf = tmp_path / "qwen3.8-27b-q8.gguf"
    gguf.write_bytes(b"0" * 1000)
    _mock_get(monkeypatch, {
        "/health": (200, {"status": "ok"}),
        "/v1/models": (200, {"data": [{"id": "qwen3.8-27b-q8-llamacpp"}]}),
        "/props": (200, {"n_ctx": 32768, "model_path": str(gguf)}),
        "/slots": (200, [{"id": 0, "is_processing": True}]),
    })
    out = rp.probe_llama_cpp(ROOT)
    assert out["available"] is True
    assert out["healthy"] is True
    assert out["model"] == "qwen3.8-27b-q8-llamacpp"
    assert out["context_length"] == 32768
    assert out["footprint_bytes"] == 1000
    assert out["footprint_measured"] is True
    assert out["generating"] is True


def test_probe_still_loading_is_available_but_not_healthy(monkeypatch):
    _mock_get(monkeypatch, {"/health": (503, {"status": "loading"})})
    out = rp.probe_llama_cpp(ROOT)
    assert out["available"] is True
    assert out["healthy"] is False
    assert out["model"] == ""


def test_probe_endpoint_down_degrades_to_unknown_without_raising(monkeypatch):
    _mock_get(monkeypatch, {"/health": None})
    out = rp.probe_llama_cpp(ROOT)
    assert out == {
        "available": False, "healthy": False, "model": "", "context_length": 0,
        "footprint_bytes": None, "footprint_measured": False, "generating": False,
    }


def test_probe_missing_model_path_leaves_footprint_unknown(monkeypatch):
    _mock_get(monkeypatch, {
        "/health": (200, {"status": "ok"}),
        "/v1/models": (200, {"data": [{"id": "m"}]}),
        "/props": (200, {"n_ctx": 8192, "model_path": "/does/not/exist.gguf"}),
    })
    out = rp.probe_llama_cpp(ROOT)
    assert out["available"] is True
    assert out["footprint_bytes"] is None
    assert out["footprint_measured"] is False


def test_probe_result_is_cached_briefly(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout=None, verify=None):
        calls["n"] += 1
        if url.endswith("/health"):
            return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", url))
        return httpx.Response(404, json={}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)
    rp.probe_llama_cpp(ROOT)
    calls_after_first = calls["n"]
    rp.probe_llama_cpp(ROOT)
    assert calls["n"] == calls_after_first  # served from cache, no new HTTP calls
    rp.probe_llama_cpp(ROOT, force=True)
    assert calls["n"] > calls_after_first


def test_root_from_base_strips_the_path():
    assert rp.root_from_base("http://127.0.0.1:8081/v1") == "http://127.0.0.1:8081"
    assert rp.root_from_base("not a url") is None


def test_external_runner_snapshot_tags_each_row_with_its_endpoint(monkeypatch):
    monkeypatch.setattr(rp, "list_external_runner_endpoints", lambda **kw: [
        {"id": "ep1", "name": "llama.cpp (8081)", "base_url": ROOT + "/v1", "root": ROOT},
        {"id": "ep2", "name": "dead endpoint", "base_url": "http://127.0.0.1:9000/v1", "root": "http://127.0.0.1:9000"},
    ])

    def fake_probe(root, timeout=None, force=False):
        if root == ROOT:
            return {"available": True, "healthy": True, "model": "qwen3.8-27b-q8-llamacpp",
                    "context_length": 32768, "footprint_bytes": 29_000_000_000,
                    "footprint_measured": True, "generating": False}
        return {"available": False, "healthy": False, "model": "", "context_length": 0,
                "footprint_bytes": None, "footprint_measured": False, "generating": False}
    monkeypatch.setattr(rp, "probe_llama_cpp", fake_probe)

    out = rp.external_runner_snapshot()
    assert len(out) == 1  # the dead endpoint (no model, not available) is left out
    assert out[0]["endpoint_id"] == "ep1"
    assert out[0]["endpoint_name"] == "llama.cpp (8081)"
    assert out[0]["engine"] == "llama.cpp"
    assert out[0]["unloadable"] is False
    assert out[0]["model"] == "qwen3.8-27b-q8-llamacpp"


def test_is_same_machine():
    assert rp.is_same_machine("http://127.0.0.1:8081") is True
    assert rp.is_same_machine("http://localhost:8081") is True
    assert rp.is_same_machine("http://192.168.1.20:8081") is False
