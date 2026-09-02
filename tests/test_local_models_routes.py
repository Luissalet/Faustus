"""Settings → Local models: the Ollama model manager (routes/local_models_routes.py).

Driven through the real router with a fake Ollama behind httpx's
MockTransport: /api/tags, /api/ps, /api/show, /api/pull (NDJSON stream),
/api/delete and /api/generate. The card is the reference box — an RTX 4070 Ti
with 12 GB — so the fit verdicts are the ones the picker already gives.
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
from src import model_load_options as mlo
from src import settings as settings_mod
from src import local_model_catalog as catalog

MIB = 1024 ** 2
GIB = 1024 ** 3
ROOT = "http://127.0.0.1:11434"

_TAGS = [
    {"name": "qwen3.5:9b", "size": int(6.6 * GIB), "digest": "aaa111",
     "modified_at": "2026-08-01T10:00:00Z",
     "details": {"family": "qwen3", "parameter_size": "9B", "quantization_level": "Q4_K_M"}},
    {"name": "qwen3.8:27b-q8_0", "size": 29_000_000_000, "digest": "bbb222",
     "modified_at": "2026-07-01T10:00:00Z",
     "details": {"family": "qwen3", "parameter_size": "27B", "quantization_level": "Q8_0"}},
    {"name": "nomic-embed-text:latest", "size": 274_000_000, "digest": "ccc333",
     "details": {"family": "nomic-bert", "parameter_size": "137M", "quantization_level": "F16"}},
]

_SHOW = {
    "qwen3.5:9b": {
        "capabilities": ["completion", "tools", "thinking"],
        "license": "Apache License\nVersion 2.0, January 2004",
        "details": {"family": "qwen3", "parameter_size": "9B", "quantization_level": "Q4_K_M"},
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 262144},
    },
    "qwen3.8:27b-q8_0": {
        "capabilities": ["completion", "tools", "thinking", "vision"],
        "license": "",
        "details": {"family": "qwen3", "parameter_size": "27B", "quantization_level": "Q8_0"},
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 131072},
    },
    "nomic-embed-text:latest": {
        "capabilities": ["embedding"],
        "license": "Apache-2.0",
        "details": {"family": "nomic-bert"},
        "model_info": {"general.architecture": "nomic-bert", "nomic-bert.context_length": 8192},
    },
}

_PS = [
    {"name": "qwen3.5:9b", "model": "qwen3.5:9b", "size": int(8.2 * GIB), "size_vram": int(8.2 * GIB),
     "digest": "aaa111", "expires_at": "2099-01-01T00:00:00Z", "context_length": 32768,
     "details": {"parameter_size": "9B", "quantization_level": "Q4_K_M"}},
]

_4070TI = {"supported": True, "name": "NVIDIA GeForce RTX 4070 Ti",
           "total": 12282 * MIB, "used": 8700 * MIB, "free": 3582 * MIB}


class FakeOllama:
    """An Ollama behind httpx.MockTransport. `pull_lines` is the NDJSON the
    pull stream emits; `pull_gate` (an Event) holds the stream open until the
    test releases it so cancel/re-attach can be exercised."""

    def __init__(self, tags=None, ps=None, show=None):
        self.tags = list(tags if tags is not None else _TAGS)
        self.ps = list(ps if ps is not None else _PS)
        self.show = dict(show if show is not None else _SHOW)
        self.calls: list = []
        self.show_calls = 0
        self.pull_lines: list = []
        self.pull_gate: threading.Event | None = None
        self.pull_status = 200
        self.deleted: list = []
        self.generate: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = {}
        if path == "/api/tags":
            return httpx.Response(200, json={"models": self.tags})
        if path == "/api/ps":
            return httpx.Response(200, json={"models": self.ps})
        if path == "/api/show":
            self.show_calls += 1
            data = self.show.get(body.get("model") or body.get("name"))
            if data is None:
                return httpx.Response(404, json={"error": "model not found"})
            return httpx.Response(200, json=data)
        if path == "/api/delete":
            name = body.get("model") or body.get("name")
            if not any(m["name"] == name for m in self.tags):
                return httpx.Response(404, json={"error": f"model '{name}' not found"})
            self.deleted.append(name)
            self.tags = [m for m in self.tags if m["name"] != name]
            return httpx.Response(200, json={})
        if path in ("/api/generate", "/api/embed"):
            self.generate.append((path, body))
            name = body.get("model")
            if not any(m["name"] == name for m in self.tags):
                return httpx.Response(404, json={"error": f"model '{name}' not found"})
            if path == "/api/generate" and name.startswith("nomic"):
                return httpx.Response(400, json={"error": f"'{name}' does not support generate"})
            return httpx.Response(200, json={"model": name, "done": True, "done_reason": "load"})
        if path == "/api/pull":
            if self.pull_status != 200:
                return httpx.Response(self.pull_status, json={"error": "pull model manifest: file does not exist"})
            lines = list(self.pull_lines)
            gate = self.pull_gate

            def _gen():
                for i, ev in enumerate(lines):
                    if gate is not None and i == len(lines) - 1:
                        gate.wait(10)
                    yield (json.dumps(ev) + "\n").encode()
            return httpx.Response(200, content=_gen(), headers={"content-type": "application/x-ndjson"})
        return httpx.Response(404, json={"error": f"no route {path}"})


@pytest.fixture
def env(monkeypatch, tmp_path):
    fake = FakeOllama()
    monkeypatch.setattr(lm, "_client_factory",
                        lambda timeout=10.0: httpx.Client(transport=httpx.MockTransport(fake.handler), timeout=timeout))
    monkeypatch.setattr(lm, "list_ollama_endpoints", lambda include_default=True, **kw: [
        {"id": "local-ollama", "name": "Ollama", "base_url": ROOT + "/v1", "root": ROOT, "same_machine": True},
        {"id": "lan-ollama", "name": "Studio box", "base_url": "http://192.168.1.20:11434/v1",
         "root": "http://192.168.1.20:11434", "same_machine": False},
    ])
    monkeypatch.setattr(lm.gpu_shared_memory, "vram_snapshot", lambda: dict(_4070TI))
    monkeypatch.setattr(lm, "_disk", lambda root, same: {"path": "/models", "free_bytes": 50 * GIB, "total_bytes": 100 * GIB} if same else {})
    monkeypatch.setattr(mw, "auth_disabled", lambda: False)
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(settings_file))
    settings_mod._invalidate_caches()
    lm.reset_show_cache()
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


ADMIN = {"x-user": "root"}
USER = {"x-user": "alice"}


# ── listing ─────────────────────────────────────────────────────────────────

def test_list_merges_tags_show_ps_and_fit(env):
    client, fake = env
    r = client.get("/api/local-models", headers=USER)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["endpoint_id"] == "local-ollama"
    assert [e["id"] for e in data["endpoints"]] == ["local-ollama", "lan-ollama"]
    assert data["reachable"] is True
    by_name = {m["name"]: m for m in data["models"]}
    small = by_name["qwen3.5:9b"]
    assert small["capabilities"] == {"vision": False, "tools": True, "thinking": True, "embedding": False, "completion": True}
    assert small["context_length"] == 262144
    assert small["license"] == "Apache License"
    assert small["quantization"] == "Q4_K_M" and small["parameter_size"] == "9B"
    assert small["loaded"] is True
    assert small["digest"] == "aaa111"
    big = by_name["qwen3.8:27b-q8_0"]
    assert big["capabilities"]["vision"] is True
    assert big["fit"]["state"] == "over"
    assert "does not fit" in big["fit"]["note"]
    assert small["fit"]["state"] == "fits"     # the runner's own 8.2 GB is not counted against it
    embed = by_name["nomic-embed-text:latest"]
    assert embed["capabilities"]["embedding"] is True
    # loaded row: VRAM/CPU split and the expiry
    assert data["loaded"][0]["name"] == "qwen3.5:9b"
    assert data["loaded"][0]["gpu_pct"] == 100 and data["loaded"][0]["size_cpu"] == 0
    assert data["loaded"][0]["expires_at"].startswith("2099")
    # the card, with the runner's footprint separated from everyone else's
    assert data["vram"]["supported"] is True
    assert data["vram"]["held_by_runner_bytes"] == int(8.2 * GIB)
    assert data["vram"]["other_bytes"] == max(0, 8700 * MIB - int(8.2 * GIB))
    assert data["disk"]["free_bytes"] == 50 * GIB
    assert data["pulls"] == []
    # loaded models sort first
    assert data["models"][0]["name"] == "qwen3.5:9b"


def test_show_is_cached_per_digest(env):
    client, fake = env
    client.get("/api/local-models", headers=USER)
    first = fake.show_calls
    assert first == 3
    client.get("/api/local-models", headers=USER)
    assert fake.show_calls == first, "/api/show is slow: one call per blob, ever"
    # a new blob under the same name is a new answer
    fake.tags[0] = dict(fake.tags[0], digest="zzz999")
    client.get("/api/local-models", headers=USER)
    assert fake.show_calls == first + 1


def test_list_requires_a_signed_in_user(env):
    client, _ = env
    assert client.get("/api/local-models").status_code == 401


def test_list_picks_the_requested_endpoint_and_has_no_verdict_off_machine(env):
    client, fake = env
    r = client.get("/api/local-models?endpoint_id=lan-ollama", headers=USER)
    assert r.status_code == 200
    data = r.json()
    assert data["endpoint_id"] == "lan-ollama"
    assert data["vram"]["supported"] is False
    assert all(m["fit"] == {} for m in data["models"])
    assert data["disk"] == {}
    assert client.get("/api/local-models?endpoint_id=nope", headers=USER).status_code == 404


def test_list_survives_an_unreachable_ollama(env, monkeypatch):
    client, fake = env

    def _down(request):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(lm, "_client_factory",
                        lambda timeout=10.0: httpx.Client(transport=httpx.MockTransport(_down)))
    r = client.get("/api/local-models", headers=USER)
    assert r.status_code == 200
    data = r.json()
    assert data["reachable"] is False
    assert "unreachable" in data["error"]
    assert data["models"] == [] and data["loaded"] == []
    assert data["vram"]["supported"] is True     # the card is still a fact


def test_endpoint_detection_by_url(monkeypatch):
    rows = [
        SimpleNamespace(id="a", name="Ollama", base_url="http://localhost:11434/v1", is_enabled=True),
        SimpleNamespace(id="b", name="LM Studio", base_url="http://localhost:1234/v1", is_enabled=True),
        SimpleNamespace(id="c", name="Cloud", base_url="https://ollama.com/v1", is_enabled=True),
        SimpleNamespace(id="d", name="Tailnet", base_url="http://100.64.0.9:11434", is_enabled=True),
        SimpleNamespace(id="e", name="Dup", base_url="http://localhost:11434", is_enabled=True),
    ]

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            return _Q()

        def close(self):
            pass

    monkeypatch.setattr(lm, "SessionLocal", lambda: _Db())
    eps = lm.list_ollama_endpoints()
    assert [e["id"] for e in eps] == ["a", "d"]           # LM Studio, Cloud and the duplicate root are out
    assert eps[0]["root"] == "http://localhost:11434" and eps[0]["same_machine"] is True
    assert eps[1]["same_machine"] is False


def test_endpoint_visibility_follows_ownership_like_api_models(monkeypatch):
    """A regular user sees shared (owner-less) Ollama endpoints and their own;
    an admin sees everyone's."""
    rows = [
        SimpleNamespace(id="shared", name="Ollama", base_url="http://localhost:11434/v1", is_enabled=True, owner=None),
        SimpleNamespace(id="bobs", name="Bob's box", base_url="http://10.0.0.5:11434", is_enabled=True, owner="bob"),
        SimpleNamespace(id="alices", name="Alice's box", base_url="http://10.0.0.6:11434", is_enabled=True, owner="alice"),
    ]

    class _Q:
        def __init__(self, items):
            self.items = items

        def filter(self, *a, **k):
            return self

        def all(self):
            return self.items

    class _Db:
        def query(self, model):
            return _Q(rows)

        def close(self):
            pass

    seen = {}

    def _owner_filter(q, model_cls, user, **kw):
        seen["user"] = user
        return _Q([r for r in rows if r.owner in (None, user)])

    monkeypatch.setattr(lm, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(lm, "owner_filter", _owner_filter)
    assert [e["id"] for e in lm.list_ollama_endpoints(owner="alice", is_admin=False)] == ["shared", "alices"]
    assert seen["user"] == "alice"
    assert [e["id"] for e in lm.list_ollama_endpoints(owner="root", is_admin=True)] == ["shared", "bobs", "alices"]


def test_endpoint_fallback_to_env_ollama_when_nothing_is_configured(monkeypatch):
    class _Db:
        def query(self, model):
            raise RuntimeError("no table")

        def close(self):
            pass

    monkeypatch.setattr(lm, "SessionLocal", lambda: _Db())
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    eps = lm.list_ollama_endpoints()
    assert len(eps) == 1 and eps[0]["id"] == mlo.DEFAULT_ENDPOINT_ID
    assert eps[0]["root"] == "http://127.0.0.1:11434" and eps[0]["same_machine"] is True
    assert lm.list_ollama_endpoints(include_default=False) == []


# ── pulls ───────────────────────────────────────────────────────────────────

_PULL = [
    {"status": "pulling manifest"},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 1000, "completed": 100},
    {"status": "pulling aaa", "digest": "sha256:aaa", "total": 1000, "completed": 1000},
    {"status": "pulling bbb", "digest": "sha256:bbb", "total": 500, "completed": 500},
    {"status": "verifying sha256 digest"},
    {"status": "writing manifest"},
    {"status": "success"},
]


def _wait(pred, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_pull_streams_sse_progress_and_finishes(env):
    client, fake = env
    # The fake answers instantly, so hold the last line back for a moment:
    # the stream is a view on the job's state, and we want to see it move.
    fake.pull_gate = threading.Event()
    fake.pull_lines = _PULL
    threading.Timer(0.6, fake.pull_gate.set).start()
    with client.stream("POST", "/api/local-models/pull", json={"endpoint_id": "local-ollama", "name": "gemma3:4b"},
                       headers=ADMIN) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: {}":
                events.append(json.loads(line[6:]))
    assert events[0]["name"] == "gemma3:4b" and events[0]["endpoint_id"] == "local-ollama"
    statuses = [e["status_text"] for e in events]
    assert "writing manifest" in statuses and statuses[-1] == "success"
    mid = next(e for e in events if e["status_text"] == "writing manifest")
    assert mid["active"] is True and mid["status"] == "pulling"
    assert mid["completed"] == 1500 and mid["total"] == 1500
    last = events[-1]
    assert last["status"] == "done" and last["active"] is False
    assert last["total"] == 1500 and last["completed"] == 1500 and last["percent"] == 100.0
    assert last["digest"] == "sha256:bbb"
    # the Ollama request carried the model name and asked for a stream
    assert ("POST", "/api/pull") in fake.calls


def test_pull_survives_the_client_leaving_and_reattaches_via_pulls(env):
    client, fake = env
    fake.pull_gate = threading.Event()
    fake.pull_lines = _PULL
    r = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "phi4:14b"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    job = r.json()["pull"]
    assert r.json()["created"] is True
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status_text"] == "writing manifest")
    # nobody is listening; the job is still there with its progress
    listed = client.get("/api/local-models/pulls?endpoint_id=local-ollama", headers=USER).json()["pulls"]
    assert [p["id"] for p in listed] == [job["id"]]
    assert listed[0]["active"] is True and listed[0]["completed"] == 1500
    # a second pull of the same model is the same job, not a duplicate
    r2 = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "phi4:14b"},
                     headers=ADMIN)
    assert r2.json()["created"] is False and r2.json()["pull"]["id"] == job["id"]
    # re-attach by EventSource
    fake.pull_gate.set()
    with client.stream("GET", f"/api/local-models/pulls/{job['id']}/events", headers=USER) as s:
        got = [json.loads(l[6:]) for l in s.iter_lines() if l.startswith("data: ") and l != "data: {}"]
    assert got[-1]["status"] == "done"
    # done pulls stay listed for the reopened page
    listed = client.get("/api/local-models/pulls", headers=USER).json()["pulls"]
    assert listed[0]["status"] == "done"
    assert client.get("/api/local-models/pulls/nope/events", headers=USER).status_code == 404


def test_pull_can_be_cancelled(env):
    client, fake = env
    fake.pull_gate = threading.Event()
    fake.pull_lines = _PULL
    job = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "llama3.2:3b"},
                      headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["completed"] == 1500)
    assert client.delete(f"/api/local-models/pulls/{job['id']}", headers=USER).status_code == 403
    r = client.delete(f"/api/local-models/pulls/{job['id']}", headers=ADMIN)
    assert r.status_code == 200
    fake.pull_gate.set()
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status"] == "cancelled")
    assert client.delete("/api/local-models/pulls/nope", headers=ADMIN).status_code == 404


def test_a_cancelled_pull_is_not_deduplicated_against_a_new_pull_of_the_same_model(env):
    """Audited: cancel, then pull again right away → the route handed back the
    job being cancelled (still "pulling" until the stream notices the flag),
    the page attached to it and watched it end as "cancelled": no new pull."""
    client, fake = env
    fake.pull_gate = threading.Event()
    fake.pull_lines = _PULL
    j1 = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "llama3.2:3b"},
                     headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(j1["id"]).snapshot()["completed"] == 1500)
    assert client.delete(f"/api/local-models/pulls/{j1['id']}", headers=ADMIN).status_code == 200
    r2 = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "llama3.2:3b"},
                     headers=ADMIN).json()
    assert r2["created"] is True
    assert r2["pull"]["id"] != j1["id"]
    assert r2["pull"]["active"] is True and r2["pull"]["status"] in ("queued", "pulling")
    # the third one IS the second (an honest active pull is still deduplicated)
    r3 = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "llama3.2:3b"},
                     headers=ADMIN).json()
    assert r3["created"] is False and r3["pull"]["id"] == r2["pull"]["id"]
    fake.pull_gate.set()
    assert _wait(lambda: lm.pulls.get(j1["id"]).snapshot()["status"] == "cancelled")
    assert _wait(lambda: lm.pulls.get(r2["pull"]["id"]).snapshot()["status"] == "done")
    assert sum(1 for m, p in fake.calls if p == "/api/pull") == 2
    # the listing shows the cancelled one as not active
    listed = {p["id"]: p for p in client.get("/api/local-models/pulls?endpoint_id=local-ollama", headers=USER).json()["pulls"]}
    assert listed[j1["id"]]["status"] == "cancelled" and listed[j1["id"]]["active"] is False


def test_pull_reports_ollama_errors(env):
    client, fake = env
    fake.pull_lines = [{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}]
    job = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "nope:1b"},
                      headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status"] == "error")
    assert "does not exist" in lm.pulls.get(job["id"]).snapshot()["error"]
    fake.pull_status = 404
    job = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "nope:2b"},
                      headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status"] == "error")
    assert "HTTP 404" in lm.pulls.get(job["id"]).snapshot()["error"]


def test_pull_refuses_a_layer_bigger_than_the_free_disk(env, monkeypatch):
    client, fake = env
    monkeypatch.setattr(lm, "_disk", lambda root, same: {"free_bytes": 800, "total_bytes": 10 ** 12})
    fake.pull_lines = _PULL
    job = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": "big:1"},
                      headers=ADMIN).json()["pull"]
    assert _wait(lambda: lm.pulls.get(job["id"]).snapshot()["status"] == "error")
    assert "free disk" in lm.pulls.get(job["id"]).snapshot()["error"]


@pytest.mark.parametrize("bad", ["", "  ", "qwen3.5:9b; rm -rf /", "../etc", "/abs", "a b", "x" * 201, "ok/../x", "-flag"])
def test_pull_validates_the_model_name(env, bad):
    client, _ = env
    r = client.post("/api/local-models/pull?stream=false", json={"endpoint_id": "local-ollama", "name": bad}, headers=ADMIN)
    assert r.status_code == 400, bad


@pytest.mark.parametrize("good", ["qwen3.5:9b", "hf.co/unsloth/Qwen3-8B-GGUF:Q4_K_M", "nomic-embed-text", "user/model:tag", "gemma3n:e4b"])
def test_pull_accepts_real_names(good):
    assert lm.validate_model_name(good) == good


def test_pull_is_admin_only(env):
    client, _ = env
    r = client.post("/api/local-models/pull", json={"endpoint_id": "local-ollama", "name": "qwen3:8b"}, headers=USER)
    assert r.status_code == 403
    assert client.post("/api/local-models/pull", json={"name": "qwen3:8b"}).status_code == 403


# ── delete / load / unload ──────────────────────────────────────────────────

def test_delete_removes_the_model_and_its_saved_options(env):
    client, fake = env
    client.put("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama", json={"num_ctx": 16384}, headers=ADMIN)
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {"num_ctx": 16384}
    assert client.delete("/api/local-models/qwen3.5:9b?endpoint_id=local-ollama", headers=USER).status_code == 403
    r = client.delete("/api/local-models/qwen3.5:9b?endpoint_id=local-ollama", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert fake.deleted == ["qwen3.5:9b"]
    assert mlo.get_options("local-ollama", "qwen3.5:9b") == {}
    assert client.delete("/api/local-models/qwen3.5:9b?endpoint_id=local-ollama", headers=ADMIN).status_code == 404
    # names with a slash reach the route whole
    fake.tags.append({"name": "hf.co/user/repo:Q4", "size": 1, "digest": "d"})
    r = client.delete("/api/local-models/hf.co/user/repo:Q4?endpoint_id=local-ollama", headers=ADMIN)
    assert r.status_code == 200 and fake.deleted[-1] == "hf.co/user/repo:Q4"


def test_unload_sends_keep_alive_zero_and_load_warms_with_the_saved_default(env):
    client, fake = env
    r = client.post("/api/local-models/unload", json={"endpoint_id": "local-ollama", "name": "qwen3.5:9b"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert fake.generate[-1] == ("/api/generate", {"model": "qwen3.5:9b", "keep_alive": 0})
    r = client.post("/api/local-models/load", json={"endpoint_id": "local-ollama", "name": "qwen3.5:9b"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["keep_alive"] == "5m"
    assert fake.generate[-1][1]["keep_alive"] == "5m"
    mlo.set_options("local-ollama", "qwen3.5:9b", {"keep_alive": "1h"})
    client.post("/api/local-models/load", json={"endpoint_id": "local-ollama", "name": "qwen3.5:9b"}, headers=ADMIN)
    assert fake.generate[-1][1]["keep_alive"] == "1h"
    # an explicit keep_alive in the request wins, and is validated
    client.post("/api/local-models/load", json={"endpoint_id": "local-ollama", "name": "qwen3.5:9b", "keep_alive": "-1"}, headers=ADMIN)
    assert fake.generate[-1][1]["keep_alive"] == "-1"
    # embedding models only answer /api/embed
    r = client.post("/api/local-models/load", json={"endpoint_id": "local-ollama", "name": "nomic-embed-text:latest"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["via"] == "/api/embed"
    assert client.post("/api/local-models/load", json={"endpoint_id": "local-ollama", "name": "ghost:1b"}, headers=ADMIN).status_code == 404
    assert client.post("/api/local-models/unload", json={"name": "qwen3.5:9b"}, headers=USER).status_code == 403


@pytest.mark.parametrize("keep_alive", ["soon", True, "10 minutes", [1]])
def test_load_rejects_a_bad_keep_alive_with_400_not_500(env, keep_alive):
    """Audited: sanitize_options raises ValueError, which the route let
    escape as a 500 (the page showed 'load failed: HTTP 500')."""
    client, fake = env
    r = client.post("/api/local-models/load", json={"endpoint_id": "local-ollama", "name": "qwen3.5:9b", "keep_alive": keep_alive},
                    headers=ADMIN)
    assert r.status_code == 400, r.text
    assert "keep_alive" in r.json()["detail"]
    assert not fake.generate


def test_pick_endpoint_needs_the_endpoint_list_it_is_given():
    """The `endpoints is None` branch referenced a `request` that did not exist
    (NameError at runtime); the helper now simply requires the list."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        lm._pick_endpoint("local-ollama")
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        lm._pick_endpoint("local-ollama", [])
    assert e.value.status_code == 404
    eps = [{"id": "a"}, {"id": "b"}]
    assert lm._pick_endpoint(None, eps) == {"id": "a"}
    assert lm._pick_endpoint("b", eps) == {"id": "b"}
    with pytest.raises(HTTPException) as e:
        lm._pick_endpoint("c", eps)
    assert e.value.status_code == 404


# ── per-model options ───────────────────────────────────────────────────────

def test_options_roundtrip_and_validation(env):
    client, _ = env
    r = client.get("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama", headers=USER)
    assert r.status_code == 200 and r.json()["options"] == {}
    r = client.put("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama",
                   json={"num_ctx": "32768", "num_gpu": 40, "keep_alive": "30m", "temperature": 0.2, "evil": 1}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["options"] == {"num_ctx": 32768, "num_gpu": 40, "keep_alive": "30m"}
    assert settings_mod.load_settings()["model_load_options"] == {"local-ollama|qwen3.5:9b": {"num_ctx": 32768, "num_gpu": 40, "keep_alive": "30m"}}
    r = client.get("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama", headers=USER)
    assert r.json()["options"]["num_ctx"] == 32768
    # the listing shows them on the row
    row = next(m for m in client.get("/api/local-models", headers=USER).json()["models"] if m["name"] == "qwen3.5:9b")
    assert row["options"] == {"num_ctx": 32768, "num_gpu": 40, "keep_alive": "30m"}
    # bad values are refused, not clamped into something else
    for bad in ({"num_ctx": 12}, {"num_ctx": "lots"}, {"num_gpu": -1}, {"keep_alive": "soon"}, {"keep_alive": True}):
        assert client.put("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama", json=bad, headers=ADMIN).status_code == 400, bad
    # empty object clears the entry
    r = client.put("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama", json={"num_ctx": ""}, headers=ADMIN)
    assert r.json()["options"] == {}
    assert settings_mod.load_settings()["model_load_options"] == {}
    assert client.put("/api/local-models/qwen3.5:9b/options?endpoint_id=local-ollama", json={"num_ctx": 4096}, headers=USER).status_code == 403


def test_model_load_options_default_is_an_empty_table():
    assert settings_mod.DEFAULT_SETTINGS["model_load_options"] == {}


# ── discover ────────────────────────────────────────────────────────────────

def test_discover_filters_the_catalogue_and_annotates_fit(env):
    client, fake = env
    r = client.get("/api/local-models/discover?q=qwen", headers=USER)
    assert r.status_code == 200
    data = r.json()
    names = [i["name"] for i in data["items"]]
    assert "qwen3.5" in names and "qwen3-coder" in names and "llama3.2" not in names
    q = next(i for i in data["items"] if i["name"] == "qwen3.5")
    tags = {t["tag"]: t for t in q["tags"]}
    assert tags["9b"]["name"] == "qwen3.5:9b" and tags["9b"]["installed"] is True
    assert tags["9b"]["fit"]["state"] == "fits"
    assert tags["27b-q8_0"]["fit"]["state"] == "over"
    assert tags["27b-q8_0"]["installed"] is False       # `qwen3.8:27b-q8_0` is a different model
    assert data["approximate"] is True
    # multi-term search, capability terms, and the empty query
    assert [i["name"] for i in client.get("/api/local-models/discover?q=embedding nomic", headers=USER).json()["items"]] == ["nomic-embed-text"]
    assert len(client.get("/api/local-models/discover", headers=USER).json()["items"]) == len(catalog.CATALOG)
    assert client.get("/api/local-models/discover?q=zzzz-not-a-model", headers=USER).json()["items"] == []


def test_discover_has_no_verdict_without_a_card(env, monkeypatch):
    client, _ = env
    monkeypatch.setattr(lm.gpu_shared_memory, "vram_snapshot", lambda: {"supported": False, "reason": "no nvidia-smi"})
    data = client.get("/api/local-models/discover?q=gemma3", headers=USER).json()
    assert data["vram"]["supported"] is False
    assert all(t["fit"] == {} for i in data["items"] for t in i["tags"])
    # off-machine endpoints do not get one either
    monkeypatch.setattr(lm.gpu_shared_memory, "vram_snapshot", lambda: dict(_4070TI))
    data = client.get("/api/local-models/discover?q=gemma3&endpoint_id=lan-ollama", headers=USER).json()
    assert all(t["fit"] == {} for i in data["items"] for t in i["tags"])


def test_catalogue_is_well_formed_and_reasonably_sized():
    assert len(catalog.CATALOG) >= 40
    seen = set()
    for entry in catalog.CATALOG:
        assert entry["name"] not in seen
        seen.add(entry["name"])
        assert entry["tags"], entry["name"]
        assert entry["default_tag"] in {t["tag"] for t in entry["tags"]}, entry["name"]
        assert set(entry["capabilities"]) <= {"vision", "tools", "thinking", "embedding"}
        for t in entry["tags"]:
            assert t["gb"] > 0 and t["params"]
    for must in ("qwen3.5", "qwen3-coder", "llama3.1", "gemma3", "mistral", "phi4", "deepseek-r1", "nomic-embed-text", "llava"):
        assert must in seen
    assert catalog.full_name(catalog.CATALOG[0], {"tag": "9b"}) == "qwen3.5:9b"
    assert catalog.full_name({"name": "nomic-embed-text"}, {"tag": "latest"}) == "nomic-embed-text"


def test_discover_verdict_ignores_what_is_currently_loaded(env):
    """A pull is a decision about later: the verdict is against the empty card."""
    client, _ = env
    v = client.get("/api/local-models/discover?q=gemma3", headers=USER).json()["vram"]
    assert v["clean_budget_bytes"] == 12282 * MIB - lm._FIT_RESERVE_BYTES


# ── the whole thing is wired into the app ───────────────────────────────────

def test_app_registers_the_router():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    assert "from routes.local_models_routes import setup_local_models_routes" in src
    assert "app.include_router(setup_local_models_routes())" in src
