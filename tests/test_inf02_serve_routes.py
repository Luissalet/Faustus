"""routes/inference_routes.py + the INF-02 additions to
routes/cookbook_routes.py::model_serve — INF-02 A5.

`model_serve` is exercised the way `tests/test_cookbook_hf_token_grant.py`
already does: call the FastAPI endpoint function directly (not through
TestClient/uvicorn), with `require_admin` and the tmux/binary probes
monkeypatched out, and `asyncio.create_subprocess_shell` faked so nothing
ever touches a real process. `routes/inference_routes.py`'s three endpoints
are simple enough to go through a real `TestClient`, following the same
`_client()` pattern `tests/test_capability_system_routes.py` uses.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
import routes.inference_routes as inference_routes
from routes.cookbook_helpers import ServeRequest
from src import launch_receipts
from src.contracts.inference import EngineIdentity, LaunchReceipt


# ── shared harness for cookbook_routes.model_serve ──────────────────────────

@pytest.fixture
def staging(tmp_path, monkeypatch):
    directory = tmp_path / "odysseus-tmux"
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", directory)
    monkeypatch.setattr(cookbook_routes, "_staging_dirs_restricted", set(), raising=False)
    return directory


@pytest.fixture(autouse=True)
def _admin_and_no_probes(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    # Pin the tmux launch path on every platform: on a real Windows host
    # `model_serve` would otherwise take `_launch_local_detached` (a real
    # detached process — seen on Luis's machine, 12-09-2026) and the
    # `fake_launch` shell stub would never see the launch. Same trick
    # `tests/test_cookbook_hf_token_grant.py` uses.
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)

    async def _available(*args, **kwargs):
        return True

    monkeypatch.setattr(cookbook_routes, "_binary_available", _available)


@pytest.fixture(autouse=True)
def _receipts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_receipts, "RECEIPTS_DIR", str(tmp_path / "launch_receipts"))


class _FakeStream:
    def __init__(self, data: bytes = b""):
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data


class _FakeProc:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture
def fake_launch(monkeypatch):
    calls = []

    async def fake_shell(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)
    return calls


def _endpoint(path: str, method: str = "POST"):
    router = cookbook_routes.setup_cookbook_routes()
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} {path} route not found")


def _admin_request(path: str) -> Request:
    request = Request(
        {"type": "http", "method": "POST", "path": path, "headers": [], "state": {}}
    )
    request.state.current_user = "admin"
    return request


# A pip-install command keeps model_serve away from the DB-backed
# auto-register-an-endpoint path (no real model is "served"), while still
# exercising the exact same plan-assessment/receipt code every real serve
# goes through.
_PIP_SERVE = dict(repo_id="huggingface-hub", cmd="python -m pip install huggingface-hub")


# ── 409 serve.incompatible ───────────────────────────────────────────────────

async def test_serve_returns_409_for_dense_model_requesting_expert_parallel():
    resp = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(
            **_PIP_SERVE,
            plan={
                "implementation": "vllm",
                "model": "org/dense-model",
                "options": {"expert_parallel": True},
                "arch": {"kind": "dense"},
            },
        ),
    )
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["error_class"] == "serve.incompatible"
    assert body["assessments"][0]["option"] == "expert_parallel"
    assert body["assessments"][0]["support"] == "unsupported"


async def test_serve_409_leaves_no_receipt_filed():
    await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(
            **_PIP_SERVE,
            plan={
                "implementation": "vllm", "model": "org/dense-model",
                "options": {"expert_parallel": True}, "arch": {"kind": "dense"},
            },
        ),
    )
    # No session_id is even minted before the 409 short-circuit, but this
    # also guards against a future refactor silently recording one anyway.
    assert launch_receipts.get("serve-should-not-exist") is None


# ── force_manual overrides a blocker ────────────────────────────────────────

async def test_serve_launches_anyway_with_force_manual(fake_launch, staging):
    body = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(
            **_PIP_SERVE,
            plan={
                "implementation": "vllm", "model": "org/dense-model",
                "options": {"expert_parallel": True}, "arch": {"kind": "dense"},
            },
            force_manual=True,
        ),
    )
    assert body["ok"] is True
    receipt = body["receipt"]
    assert receipt["verify_state"] == "pending"
    assert receipt["plan"]["implementation"] == "vllm"
    assert receipt["assessments"][0]["option"] == "expert_parallel"
    assert receipt["assessments"][0]["support"] == "unsupported"
    assert fake_launch  # the launch really happened


# ── no plan (legacy client / manual command) ────────────────────────────────

async def test_serve_without_plan_records_manual_receipt(fake_launch, staging):
    body = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(**_PIP_SERVE),
    )
    assert body["ok"] is True
    assert body["receipt"]["plan"] == {"manual": True}
    assert body["receipt"]["assessments"] == []
    assert body["receipt"]["engine"]["generation"] == 1


async def test_serve_without_blockers_launches_and_receipt_is_readable_after(fake_launch, staging):
    body = await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(
            **_PIP_SERVE,
            plan={"implementation": "vllm", "model": "org/model", "options": {"ctx": 8192}},
        ),
    )
    assert body["ok"] is True
    session_id = body["session_id"]
    fetched = launch_receipts.get(session_id)
    assert fetched is not None
    assert fetched.plan["options"]["ctx"] == 8192
    assert fetched.assessments[0].support == "supported"


# ── routes/inference_routes.py: assess/receipt/verify ───────────────────────

def _inference_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(inference_routes, "require_admin", lambda _request: None)
    app = FastAPI()
    app.include_router(inference_routes.setup_inference_routes())
    return TestClient(app)


def test_assess_endpoint_returns_assessments_and_blockers(monkeypatch):
    client = _inference_client(monkeypatch)
    resp = client.post("/api/model/serve/assess", json={
        "implementation": "vllm",
        "options": {"expert_parallel": True, "ctx": 8192},
        "arch": {"kind": "dense"},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["assessments"]) == 2
    assert body["blockers"] == [b for b in body["assessments"] if b["option"] == "expert_parallel"]
    assert body["blockers"][0]["support"] == "unsupported"


def test_assess_endpoint_rejects_unknown_implementation_vocabulary(monkeypatch):
    client = _inference_client(monkeypatch)
    resp = client.post("/api/model/serve/assess", json={"implementation": "not-a-real-engine", "options": {}})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "serve.invalid_plan"


def test_assess_endpoint_no_blockers_for_supported_options(monkeypatch):
    client = _inference_client(monkeypatch)
    resp = client.post("/api/model/serve/assess", json={
        "implementation": "llama-server",
        "options": {"ctx": 8192, "ngl": 99},
    })
    assert resp.status_code == 200
    assert resp.json()["blockers"] == []


def test_receipt_endpoint_404_for_unknown_session(monkeypatch):
    client = _inference_client(monkeypatch)
    resp = client.get("/api/model/serve/serve-doesnotexist/receipt")
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "serve.receipt_not_found"


def test_receipt_endpoint_200_after_record(monkeypatch):
    client = _inference_client(monkeypatch)
    launch_receipts.record(
        "serve-zzzzzzzz", engine=EngineIdentity(implementation="llama-server"), model=None,
        requested_cmd="cmd", final_cmd="cmd", rewrites=[], plan={"manual": True}, assessments=[],
    )
    resp = client.get("/api/model/serve/serve-zzzzzzzz/receipt")
    assert resp.status_code == 200
    assert resp.json()["receipt"]["session_id"] == "serve-zzzzzzzz"


def test_verify_endpoint_400_when_base_url_cannot_be_inferred(monkeypatch):
    client = _inference_client(monkeypatch)
    launch_receipts.record(
        "serve-nourl001", engine=EngineIdentity(implementation="vllm"), model=None,
        requested_cmd="vllm serve org/model", final_cmd="vllm serve org/model",
        rewrites=[], plan={"implementation": "vllm", "options": {}}, assessments=[],
    )
    resp = client.post("/api/model/serve/serve-nourl001/verify", json={})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "serve.base_url_required"


def test_verify_endpoint_404_for_unknown_session(monkeypatch):
    client = _inference_client(monkeypatch)
    resp = client.post("/api/model/serve/serve-missing/verify", json={})
    assert resp.status_code == 404
    assert resp.json()["error_class"] == "serve.receipt_not_found"


def test_verify_endpoint_derives_base_url_from_port_flag(monkeypatch):
    client = _inference_client(monkeypatch)
    receipt = launch_receipts.record(
        "serve-port0001", engine=EngineIdentity(implementation="llama-server"), model=None,
        requested_cmd="llama-server --port 8080", final_cmd="llama-server --port 8080",
        rewrites=[], plan={"implementation": "llama-server", "options": {}}, assessments=[],
    )
    seen = {}

    async def fake_verify(session_id, *, base_url, authorized_probe=False):
        seen["session_id"] = session_id
        seen["base_url"] = base_url
        seen["authorized_probe"] = authorized_probe
        return dataclasses.replace(receipt, verify_state="verified", verified_at="2026-09-12T00:00:00Z")

    monkeypatch.setattr(launch_receipts, "verify", fake_verify)
    resp = client.post("/api/model/serve/serve-port0001/verify", json={})
    assert resp.status_code == 200
    assert seen["base_url"] == "http://127.0.0.1:8080"
    assert seen["authorized_probe"] is False
    assert resp.json()["receipt"]["verify_state"] == "verified"


def test_verify_endpoint_passes_explicit_base_url_and_authorized_probe(monkeypatch):
    client = _inference_client(monkeypatch)
    receipt = launch_receipts.record(
        "serve-port0002", engine=EngineIdentity(implementation="llama-server"), model=None,
        requested_cmd="llama-server --port 8080", final_cmd="llama-server --port 8080",
        rewrites=[], plan={"implementation": "llama-server", "options": {}}, assessments=[],
    )
    seen = {}

    async def fake_verify(session_id, *, base_url, authorized_probe=False):
        seen["base_url"] = base_url
        seen["authorized_probe"] = authorized_probe
        return dataclasses.replace(receipt, verify_state="verified")

    monkeypatch.setattr(launch_receipts, "verify", fake_verify)
    resp = client.post(
        "/api/model/serve/serve-port0002/verify",
        json={"base_url": "http://example-host:9999", "authorized_probe": True},
    )
    assert resp.status_code == 200
    assert seen["base_url"] == "http://example-host:9999"
    assert seen["authorized_probe"] is True


def test_infer_base_url_ollama_reuses_ollama_bind_from_cmd():
    receipt = LaunchReceipt(
        session_id="s1", engine=EngineIdentity(implementation="ollama"), model=None,
        requested_cmd="OLLAMA_HOST=0.0.0.0:11500 ollama serve",
        final_cmd="OLLAMA_HOST=0.0.0.0:11500 ollama serve",
        rewrites=(), plan={}, assessments=(), observed={}, differences=(), checks=(),
        created_at="2026-09-12T00:00:00Z",
    )
    assert inference_routes._infer_base_url(receipt) == "http://127.0.0.1:11500"


@pytest.mark.parametrize("cmd,expected", [
    ("llama-server --model x.gguf --port 8080", "llama-server"),
    ("python -m llama_cpp.server --model x.gguf", "llama_cpp.server"),
    ("vllm serve org/model --port 8000", "vllm"),
    ("python -m sglang.launch_server --model-path org/model", "sglang"),
    ("OLLAMA_HOST=0.0.0.0:11434 ollama serve", "ollama"),
    ("python -m pip install huggingface-hub", "unknown"),
    ("", "unknown"),
])
def test_infer_engine_implementation(cmd, expected):
    assert cookbook_routes._infer_engine_implementation(cmd) == expected


@pytest.mark.parametrize("cmd,expected", [
    ("llama-server --port 8080", 8080),
    ("vllm serve org/model -p 9000", 9000),
    ("ollama serve", None),
    ("llama-server --port 99999", None),
])
def test_infer_engine_port(cmd, expected):
    assert cookbook_routes._infer_engine_port(cmd) == expected


def test_infer_base_url_none_when_nothing_to_go_on():
    receipt = LaunchReceipt(
        session_id="s1", engine=EngineIdentity(implementation="vllm"), model=None,
        requested_cmd="vllm serve org/model", final_cmd="vllm serve org/model",
        rewrites=(), plan={}, assessments=(), observed={}, differences=(), checks=(),
        created_at="2026-09-12T00:00:00Z",
    )
    assert inference_routes._infer_base_url(receipt) is None
