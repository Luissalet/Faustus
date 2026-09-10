"""L43 · HW-02 (explicit GPU/RAM split + kv verdict) and MOD-04 (scoped
options + effective-resolve routes), both in routes/local_models_routes.py.

Driven through the real router with a fake Ollama behind httpx's
MockTransport, the same pattern tests/test_local_models_routes.py uses.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

import core.middleware as mw
import routes.local_models_routes as lm
from src import model_load_options as mlo
from src import settings as settings_mod

MIB = 1024 ** 2
GIB = 1024 ** 3
ROOT = "http://127.0.0.1:11434"

# On disk: 6.6 GiB. Resident (weights + KV, at a 32768 loaded context): 8.2
# GiB, all in VRAM — overhead of 1.6 GiB / 32768 tokens is a real, positive
# measured KV rate (the exact case vram_fit.kv_bytes_per_token_measured is
# for).
_TAGS = [
    {"name": "qwen3.5:9b", "size": int(6.6 * GIB), "digest": "aaa111",
     "details": {"family": "qwen3", "parameter_size": "9B", "quantization_level": "Q4_K_M"}},
]
_SHOW = {
    "qwen3.5:9b": {
        "capabilities": ["completion", "tools"],
        "details": {"family": "qwen3", "parameter_size": "9B", "quantization_level": "Q4_K_M"},
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 131072},
    },
}
_PS_SPILLING = [
    {"name": "qwen3.5:9b", "model": "qwen3.5:9b", "size": int(8.2 * GIB), "size_vram": int(6.0 * GIB),
     "digest": "aaa111", "expires_at": "2099-01-01T00:00:00Z", "context_length": 32768,
     "details": {"parameter_size": "9B", "quantization_level": "Q4_K_M"}},
]
_PS_NO_CTX = [
    {"name": "qwen3.5:9b", "model": "qwen3.5:9b", "size": int(8.2 * GIB), "size_vram": int(8.2 * GIB),
     "digest": "aaa111", "expires_at": "2099-01-01T00:00:00Z", "context_length": 0,
     "details": {"parameter_size": "9B", "quantization_level": "Q4_K_M"}},
]

_4070TI = {"supported": True, "name": "NVIDIA GeForce RTX 4070 Ti",
           "total": 12282 * MIB, "used": 8700 * MIB, "free": 3582 * MIB}


class FakeOllama:
    def __init__(self, ps):
        self.tags = list(_TAGS)
        self.ps = list(ps)
        self.show = dict(_SHOW)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/tags"):
            return httpx.Response(200, json={"models": self.tags})
        if path.endswith("/api/ps"):
            return httpx.Response(200, json={"models": self.ps})
        if path.endswith("/api/show"):
            name = request.content and __import__("json").loads(request.content).get("model")
            return httpx.Response(200, json=self.show.get(name, {}))
        return httpx.Response(404, json={"error": f"no route {path}"})


def _make_env(monkeypatch, tmp_path, ps):
    fake = FakeOllama(ps)
    monkeypatch.setattr(lm, "_client_factory",
                        lambda timeout=10.0: httpx.Client(transport=httpx.MockTransport(fake.handler), timeout=timeout))
    monkeypatch.setattr(lm, "list_ollama_endpoints", lambda include_default=True, **kw: [
        {"id": "local-ollama", "name": "Ollama", "base_url": ROOT + "/v1", "root": ROOT, "same_machine": True},
    ])
    monkeypatch.setattr(lm.gpu_shared_memory, "vram_snapshot", lambda: dict(_4070TI))
    monkeypatch.setattr(lm, "_disk", lambda root, same: {})
    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    lm.reset_show_cache()
    mlo.reset_endpoint_cache()

    app = FastAPI()
    app.include_router(lm.setup_local_models_routes())
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda u: u == "root")

    class _Stamp(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            user = request.headers.get("x-user")
            if user:
                request.state.current_user = user
            return await call_next(request)

    app.add_middleware(_Stamp)
    return TestClient(app, raise_server_exceptions=False)


ADMIN = {"x-user": "root"}
USER = {"x-user": "alice"}


@pytest.fixture
def spilling_client(monkeypatch, tmp_path):
    client = _make_env(monkeypatch, tmp_path, _PS_SPILLING)
    yield client
    settings_mod._invalidate_caches()


@pytest.fixture
def full_gpu_client(monkeypatch, tmp_path):
    client = _make_env(monkeypatch, tmp_path, [
        {"name": "qwen3.5:9b", "model": "qwen3.5:9b", "size": int(8.2 * GIB), "size_vram": int(8.2 * GIB),
         "digest": "aaa111", "expires_at": "2099-01-01T00:00:00Z", "context_length": 32768,
         "details": {"parameter_size": "9B", "quantization_level": "Q4_K_M"}},
    ])
    yield client
    settings_mod._invalidate_caches()


@pytest.fixture
def no_ctx_client(monkeypatch, tmp_path):
    client = _make_env(monkeypatch, tmp_path, _PS_NO_CTX)
    yield client
    settings_mod._invalidate_caches()


# ── HW-02 ────────────────────────────────────────────────────────────────

def test_spilling_model_shows_explicit_gpu_and_ram_gb_text(spilling_client):
    r = spilling_client.get("/api/local-models", headers=USER)
    assert r.status_code == 200, r.text
    row = r.json()["loaded"][0]
    assert row["size_vram"] == int(6.0 * GIB)
    assert row["size_cpu"] == int(8.2 * GIB) - int(6.0 * GIB)
    assert row["gpu_ram_split_text"] == "6.0 GB in GPU, 2.2 GB spilled to RAM"


def test_fully_resident_model_has_no_ram_mention(full_gpu_client):
    row = full_gpu_client.get("/api/local-models", headers=USER).json()["loaded"][0]
    assert row["size_cpu"] == 0
    assert row["gpu_ram_split_text"] == "8.2 GB in GPU"
    assert "RAM" not in row["gpu_ram_split_text"]


def test_kv_is_measured_when_file_size_and_context_are_both_known(full_gpu_client):
    row = full_gpu_client.get("/api/local-models", headers=USER).json()["loaded"][0]
    assert row["kv"]["state"] == "measured"
    assert row["kv"]["context_length"] == 32768
    assert row["kv"]["bytes_per_token"] > 0


def test_kv_is_unknown_without_a_loaded_context_never_a_guess(no_ctx_client):
    row = no_ctx_client.get("/api/local-models", headers=USER).json()["loaded"][0]
    assert row["kv"] == {"state": "unknown"}


# ── MOD-04 routes ───────────────────────────────────────────────────────

def test_scoped_options_round_trip_and_require_admin(full_gpu_client):
    client = full_gpu_client
    # Reading is fine for a plain user; writing is admin-only.
    r = client.put("/api/local-models/qwen3.5:9b/options/scoped?scope=project&scope_id=proj1",
                    json={"options": {"num_ctx": 16384}}, headers=USER)
    assert r.status_code == 403
    r = client.put("/api/local-models/qwen3.5:9b/options/scoped?scope=project&scope_id=proj1",
                    json={"options": {"num_ctx": 16384}}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["options"] == {"num_ctx": 16384}

    r = client.get("/api/local-models/qwen3.5:9b/options/scoped?scope=project&scope_id=proj1", headers=USER)
    assert r.status_code == 200
    assert r.json()["options"] == {"num_ctx": 16384}
    # A different scope_id under the same scope is a different override.
    r = client.get("/api/local-models/qwen3.5:9b/options/scoped?scope=project&scope_id=other", headers=USER)
    assert r.json()["options"] == {}


def test_scoped_options_reject_a_missing_scope_id(full_gpu_client):
    r = full_gpu_client.put("/api/local-models/qwen3.5:9b/options/scoped?scope=project&scope_id=",
                            json={"options": {"num_ctx": 16384}}, headers=ADMIN)
    assert r.status_code == 400


def test_effective_options_show_who_wins(full_gpu_client):
    client = full_gpu_client
    client.put("/api/local-models/qwen3.5:9b/options", json={"options": {"num_ctx": 8192}}, headers=ADMIN)
    client.put("/api/local-models/qwen3.5:9b/options/scoped?scope=session&scope_id=s1",
               json={"options": {"num_ctx": 65536}}, headers=ADMIN)
    r = client.get(
        "/api/local-models/qwen3.5:9b/options/effective?session_id=s1&project_id=p1", headers=USER,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["options"]["num_ctx"] == 65536
    assert body["origin"]["num_ctx"]["scope"] == "session"
    assert body["overridden"]["num_ctx"][0]["scope"] == "global"
    assert body["overridden"]["num_ctx"][0]["value"] == 8192
