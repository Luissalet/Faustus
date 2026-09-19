"""/api/system/usage carries a self-hosted OpenAI-compatible runner's model
(llama.cpp's llama-server) as `external_runners`, additive to `ollama` — the
VRAM widget's model label used to say "no model" while such a runner held
the card, because every reading here went through Ollama's own `/api/ps`.
"""
from __future__ import annotations

import asyncio

import pytest

import routes.system_usage_routes as sur

GIB = 1024 ** 3
MIB = 1024 ** 2
UUID0 = "GPU-5ab72dd9-1a45-c3af-5e12-ac7796b1def7"
ONE_CARD = f"0, NVIDIA GeForce RTX 5090, 40, 48041, 61440, 55, 250.00, 450.00, {UUID0}, 00000000:01:00.0, 13399\n"

EXTERNAL_ROW = {
    "model": "qwen3.8-27b-q8-llamacpp", "endpoint_name": "llama.cpp (8081)",
    "engine": "llama.cpp", "context_length": 32768,
    "footprint_bytes": 29 * GIB, "footprint_measured": True,
    "generating": False, "unloadable": False,
}


@pytest.fixture
def box(monkeypatch):
    async def _ollama(client):
        return {"reachable": True, "base": "http://127.0.0.1:11434", "models": []}

    async def _external():
        return [dict(EXTERNAL_ROW)]

    monkeypatch.setattr(sur, "_collect_ollama", _ollama)
    monkeypatch.setattr(sur, "_collect_external_runners", _external)
    monkeypatch.setattr(sur, "_collect_gpu", lambda: (sur.parse_gpu_query(ONE_CARD), None))
    monkeypatch.setattr(sur, "_collect_host", lambda: {"cpu": {"percent": 1.0, "count": 8}, "ram": {"used": 1, "total": 2, "percent": 50.0}})
    monkeypatch.setattr(sur.gpu_shared_memory, "collect", lambda: {"supported": False, "reason": "test"})
    monkeypatch.setattr(sur, "_collect_policy", lambda: {"exposed": False})
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
    yield
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None


def test_external_runner_shows_up_alongside_an_empty_ollama(box):
    data = asyncio.run(sur.collect_usage())
    assert data["ollama"]["reachable"] is True
    assert data["ollama"]["models"] == []
    assert data["external_runners"] == [EXTERNAL_ROW]
    # The card really shows 47ish GB used (48041 MiB) — this is the owner's
    # screenshot: no Ollama resident, but the card is not empty.
    assert data["gpu"][0]["mem_used"] == 48041.0


def test_external_runner_snapshot_failure_degrades_to_empty_list(monkeypatch):
    async def _boom():
        raise RuntimeError("endpoint query failed")

    async def _ollama(client):
        return {"reachable": False, "base": "http://127.0.0.1:11434", "models": [], "error": "connection refused"}

    monkeypatch.setattr(sur, "_collect_ollama", _ollama)

    async def _external_safe():
        try:
            return await _boom()
        except Exception:  # mirrors _collect_external_runners' own guard
            return []
    monkeypatch.setattr(sur, "_collect_external_runners", _external_safe)
    monkeypatch.setattr(sur, "_collect_gpu", lambda: ([], "nvidia-smi: not found"))
    monkeypatch.setattr(sur, "_collect_host", lambda: {"cpu": {}, "ram": {}})
    monkeypatch.setattr(sur.gpu_shared_memory, "collect", lambda: {"supported": False, "reason": "test"})
    monkeypatch.setattr(sur, "_collect_policy", lambda: {"exposed": False})
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
    data = asyncio.run(sur.collect_usage())
    assert data["external_runners"] == []
    sur._cache["ts"] = 0.0
    sur._cache["data"] = None
