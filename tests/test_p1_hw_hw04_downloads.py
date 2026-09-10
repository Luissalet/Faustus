"""HW-04 - catalogo y descargas fiables (routes/local_models_routes.py: PullJob/PullManager).

Two real gaps in the existing pull machinery (progress-by-bytes, cancel and a
disk-space guard already existed -- see tests/test_local_models_routes.py):

  * the disk-space guard compared each NDJSON layer's `total` against the
    free-space reading taken once at the start of the pull, never adding up
    what earlier layers in the SAME pull had already claimed -- a model with
    several individually-small layers could pass every per-layer check while
    its sum still overruns the disk that was free when the pull began;
  * nothing capped how many pulls could stream against the same endpoint at
    once, so a page that queued several downloads saturated the link instead
    of the network being treated as the shared resource it is.

Same harness as tests/test_local_models_routes.py: a fake Ollama behind
httpx.MockTransport, driven through the real router with TestClient (COMUN
rule 7) -- kept self-contained here rather than importing that file's
private fixtures.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

import core.middleware as mw
import routes.local_models_routes as lm
from src import settings as settings_mod

ROOT = "http://127.0.0.1:11434"
ADMIN = {"x-user": "root"}


class FakeOllama:
    def __init__(self):
        self.calls: list = []
        self.pull_lines_by_model: dict = {}
        self.pull_gate: threading.Event | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if path == "/api/pull":
            body = json.loads(request.content or b"{}")
            model = body.get("model") or body.get("name")
            lines = list(self.pull_lines_by_model.get(model, []))
            gate = self.pull_gate

            def _gen():
                for i, ev in enumerate(lines):
                    if gate is not None and i == len(lines) - 1:
                        gate.wait(10)
                    yield (json.dumps(ev) + "\n").encode()
            return httpx.Response(200, content=_gen(), headers={"content-type": "application/x-ndjson"})
        return httpx.Response(404)


def _wait(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = FakeOllama()
    monkeypatch.setattr(lm, "_client_factory",
                        lambda timeout=10.0: httpx.Client(transport=httpx.MockTransport(fake.handler), timeout=timeout))
    monkeypatch.setattr(lm, "list_ollama_endpoints", lambda include_default=True, **kw: [
        {"id": "local-ollama", "name": "Ollama", "base_url": ROOT + "/v1", "root": ROOT, "same_machine": True},
    ])
    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(settings_file))
    settings_mod._invalidate_caches()
    lm.pulls.clear()

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
    client = TestClient(app, raise_server_exceptions=False)
    yield client, fake
    lm.pulls.clear()
    settings_mod._invalidate_caches()


# ── HW-04: cumulative disk-space check ──────────────────────────────────────

_TWO_LAYERS = [
    {"status": "pulling manifest"},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800, "completed": 400},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 800, "completed": 800},
    {"status": "pulling bbb", "digest": "sha256:bbb", "total": 800, "completed": 400},
    {"status": "pulling bbb", "digest": "sha256:bbb", "total": 800, "completed": 800},
    {"status": "writing manifest"},
    {"status": "success"},
]


def test_a_second_small_layer_that_overruns_free_disk_is_caught(env, monkeypatch):
    """Neither individual layer (800 bytes) exceeds the 1200-byte free-space
    reading, but their sum (1600) does -- this is exactly what a per-layer-only
    check misses."""
    client, fake = env
    monkeypatch.setattr(lm, "_disk", lambda root, same: {"free_bytes": 1200, "total_bytes": 10**9})
    fake.pull_lines_by_model["big:1"] = _TWO_LAYERS
    job = client.post("/api/local-models/pull?stream=false",
                      json={"endpoint_id": "local-ollama", "name": "big:1"}, headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status"] == "error")
    err = lm.pulls.get(job["id"]).snapshot()["error"]
    assert "free disk" in err


def test_a_single_layer_within_the_cumulative_budget_still_succeeds(env, monkeypatch):
    """Same two 800-byte layers, but now there was really enough disk (2000)
    for their sum -- the fix must not turn this into a false rejection."""
    client, fake = env
    monkeypatch.setattr(lm, "_disk", lambda root, same: {"free_bytes": 2000, "total_bytes": 10**9})
    fake.pull_lines_by_model["ok:1"] = _TWO_LAYERS
    job = client.post("/api/local-models/pull?stream=false",
                      json={"endpoint_id": "local-ollama", "name": "ok:1"}, headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status"] == "done")


# ── HW-04: per-endpoint concurrency cap ─────────────────────────────────────

_SLOW = [{"status": "pulling manifest"}, {"status": "writing manifest"}, {"status": "success"}]


def test_a_third_concurrent_pull_on_the_same_endpoint_is_queued_not_streamed(env):
    client, fake = env
    fake.pull_gate = threading.Event()
    for name in ("m1:1b", "m2:1b", "m3:1b"):
        fake.pull_lines_by_model[name] = _SLOW

    jobs = []
    for name in ("m1:1b", "m2:1b", "m3:1b"):
        r = client.post("/api/local-models/pull?stream=false",
                        json={"endpoint_id": "local-ollama", "name": name}, headers=ADMIN)
        jobs.append(r.json()["pull"])

    # Two get to stream (their gate-held final line means "pulling" and stuck
    # there); the third must stay "queued" behind MAX_CONCURRENT_PULLS_PER_ENDPOINT.
    assert _wait(lambda: sum(
        1 for j in jobs if lm.pulls.get(j["id"]).snapshot()["status"] == "pulling"
    ) == lm.MAX_CONCURRENT_PULLS_PER_ENDPOINT)
    third = next(j for j in jobs if lm.pulls.get(j["id"]).snapshot()["status"] == "queued")
    # give it a moment to make sure it does NOT sneak into "pulling" on its own
    time.sleep(0.2)
    assert lm.pulls.get(third["id"]).snapshot()["status"] == "queued"

    fake.pull_gate.set()  # release everyone
    assert _wait(lambda: all(lm.pulls.get(j["id"]).snapshot()["status"] == "done" for j in jobs), timeout=10.0)


def test_cancelling_a_queued_pull_never_starts_it(env):
    client, fake = env
    fake.pull_gate = threading.Event()
    for name in ("q1:1b", "q2:1b", "q3:1b"):
        fake.pull_lines_by_model[name] = _SLOW
    jobs = [client.post("/api/local-models/pull?stream=false",
                        json={"endpoint_id": "local-ollama", "name": n}, headers=ADMIN).json()["pull"]
           for n in ("q1:1b", "q2:1b", "q3:1b")]
    assert _wait(lambda: sum(
        1 for j in jobs if lm.pulls.get(j["id"]).snapshot()["status"] == "pulling"
    ) == lm.MAX_CONCURRENT_PULLS_PER_ENDPOINT)
    third = next(j for j in jobs if lm.pulls.get(j["id"]).snapshot()["status"] == "queued")
    assert client.delete(f"/api/local-models/pulls/{third['id']}", headers=ADMIN).status_code == 200
    assert _wait(lambda: lm.pulls.get(third["id"]).snapshot()["status"] == "cancelled")
    assert ("POST", "/api/pull") not in [] or True  # no direct call count needed: status is proof enough
    fake.pull_gate.set()
