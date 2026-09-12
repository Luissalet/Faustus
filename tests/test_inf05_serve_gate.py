"""routes/cookbook_routes.py::model_serve — INF-05 B2: the VRAM admission
gate for Cookbook serve (§12 "un benchmark no recibe permiso para saltarse
la puerta porque sea interno" — tampoco un serve).

Same harness as `tests/test_inf02_serve_routes.py`: call the FastAPI
endpoint function directly, with `require_admin`/tmux/binary probes
monkeypatched out and `IS_WINDOWS` pinned False (so a real Windows CI host
does not take the detached-process path `fake_launch` cannot see).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.responses import JSONResponse
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_helpers import ServeRequest
from src import launch_receipts, vram_admission as va

GIB = 2**30


# ── harness (mirrors tests/test_inf02_serve_routes.py) ──────────────────────

@pytest.fixture
def staging(tmp_path, monkeypatch):
    directory = tmp_path / "odysseus-tmux"
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", directory)
    monkeypatch.setattr(cookbook_routes, "_staging_dirs_restricted", set(), raising=False)
    return directory


@pytest.fixture(autouse=True)
def _admin_and_no_probes(monkeypatch):
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    # Pin the tmux launch path on every platform (see test_inf02_serve_routes.py).
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)

    async def _available(*args, **kwargs):
        return True

    monkeypatch.setattr(cookbook_routes, "_binary_available", _available)


@pytest.fixture(autouse=True)
def _receipts_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(launch_receipts, "RECEIPTS_DIR", str(tmp_path / "launch_receipts"))


@pytest.fixture(autouse=True)
def _clean_admission_tables():
    va._RESERVATIONS.clear()
    va._PENDING.clear()
    yield
    va._RESERVATIONS.clear()
    va._PENDING.clear()


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
    request = Request({"type": "http", "method": "POST", "path": path, "headers": [], "state": {}})
    request.state.current_user = "admin"
    return request


# A real serve command (not pip-install): the admission gate only runs for
# these — a pip-install task has no model weights to check against and the
# gate is skipped for it entirely.
_SERVE = dict(repo_id="org/model", cmd="llama-server --model x.gguf --port 8080")


async def _serve(**overrides):
    return await _endpoint("/api/model/serve")(
        _admin_request("/api/model/serve"),
        ServeRequest(**{**_SERVE, **overrides}),
    )


# ── 409 serve.vram_blocked with a ticket ────────────────────────────────────

async def test_serve_returns_409_vram_blocked_with_a_ticket(monkeypatch):
    async def _fake_admit_bytes(*, label, bytes_needed, gpu_indices, owner="", mode=None,
                                timeout=None, ollama_roots=()):
        assert bytes_needed == 20 * GIB
        t = va.open_ticket("physical", label, {
            "model": label, "root": "physical", "fits": False, "residents": [],
            "suggestion": [], "shortfall_bytes": 5 * GIB, "kind": "serve",
        }, kind="serve")
        return {"decision": "blocked", "reservation_id": None, "ticket": t.id, "assessment": t.assessment}

    monkeypatch.setattr(va, "admit_bytes", _fake_admit_bytes)

    resp = await _serve(plan={"implementation": "llama-server", "model": "org/model",
                              "options": {}, "weights_bytes": 20 * GIB})
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    body = json.loads(resp.body)
    assert body["error_class"] == "serve.vram_blocked"
    assert body["ticket"]
    assert body["shortfall_bytes"] == 5 * GIB
    # Nothing was launched, no receipt filed.
    assert launch_receipts.get("serve-should-not-exist") is None


async def test_serve_blocked_via_weights_bytes_field_instead_of_plan(monkeypatch):
    """`req.weights_bytes` is the fallback for a manual/legacy launch with
    no structured `plan` at all."""
    async def _fake_admit_bytes(*, label, bytes_needed, gpu_indices, owner="", mode=None,
                                timeout=None, ollama_roots=()):
        assert bytes_needed == 9 * GIB
        t = va.open_ticket("physical", label, {"residents": [], "fits": False}, kind="serve")
        return {"decision": "blocked", "reservation_id": None, "ticket": t.id, "assessment": t.assessment}

    monkeypatch.setattr(va, "admit_bytes", _fake_admit_bytes)
    resp = await _serve(weights_bytes=9 * GIB)
    assert resp.status_code == 409
    assert json.loads(resp.body)["error_class"] == "serve.vram_blocked"


# ── admission_ticket resolved: launches, receipt carries plan.admission ────

async def test_resolved_proceed_ticket_launches_and_receipt_carries_admission(fake_launch, staging):
    ticket = va.open_ticket("physical", "org/model", {
        "model": "org/model", "root": "physical", "fits": False, "residents": [],
        "budget_alongside_bytes": 2 * GIB,
    }, kind="serve")
    va.resolve(ticket.id, action="proceed", names=[], by="admin")

    body = await _serve(
        plan={"implementation": "llama-server", "model": "org/model", "options": {}, "weights_bytes": 9 * GIB},
        admission_ticket=ticket.id,
    )
    assert body["ok"] is True
    assert fake_launch  # the launch really happened
    admission = body["receipt"]["plan"]["admission"]
    assert admission["decision"] == "proceed"
    assert admission["ticket"] == ticket.id
    assert admission["bytes_needed"] == 9 * GIB
    assert admission["budget_bytes"] == 2 * GIB


async def test_resolved_unload_ticket_launches_and_receipt_says_unload(fake_launch, staging):
    ticket = va.open_ticket("physical", "org/model", {
        "model": "org/model", "root": "physical", "fits": False,
        "residents": [{"name": "small:9b", "root": "http://127.0.0.1:11434"}],
    }, kind="serve")
    va.resolve(ticket.id, action="unload", names=["small:9b"], by="admin")

    body = await _serve(
        plan={"implementation": "llama-server", "model": "org/model", "options": {}, "weights_bytes": 9 * GIB},
        admission_ticket=ticket.id,
    )
    assert body["ok"] is True
    assert body["receipt"]["plan"]["admission"]["decision"] == "unload"


async def test_resolved_cancel_ticket_refuses_the_launch(staging):
    ticket = va.open_ticket("physical", "org/model", {"residents": [], "fits": False}, kind="serve")
    va.resolve(ticket.id, action="cancel", names=[], by="admin")

    resp = await _serve(
        plan={"implementation": "llama-server", "model": "org/model", "options": {}, "weights_bytes": 9 * GIB},
        admission_ticket=ticket.id,
    )
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 409
    assert json.loads(resp.body)["error_class"] == "serve.vram_blocked"
    assert launch_receipts.get("serve-should-not-exist") is None


# ── no bytes: unknown, never blocks ──────────────────────────────────────────

async def test_serve_without_weights_bytes_launches_with_unknown_admission(fake_launch, staging):
    body = await _serve(plan={"implementation": "llama-server", "model": "org/model", "options": {}})
    assert body["ok"] is True
    admission = body["receipt"]["plan"]["admission"]
    assert admission["decision"] == "unknown"
    assert "weights size unknown" in admission["reason"]
    assert admission["bytes_needed"] is None


# ── mode off ─────────────────────────────────────────────────────────────────

async def test_serve_admission_off_mode_skips_the_check_entirely(fake_launch, staging, monkeypatch):
    monkeypatch.setattr(va, "_mode", lambda: "off")

    body = await _serve(
        plan={"implementation": "llama-server", "model": "org/model", "options": {}, "weights_bytes": 900 * GIB},
    )
    assert body["ok"] is True
    assert body["receipt"]["plan"]["admission"]["decision"] == "off"


# ── pip-install tasks: never gated ──────────────────────────────────────────

async def test_pip_install_serve_skips_the_admission_gate_entirely(fake_launch, staging, monkeypatch):
    called = []

    async def _fake_admit_bytes(**kwargs):
        called.append(kwargs)
        return {"decision": "off", "reservation_id": None, "ticket": None, "assessment": {}}

    monkeypatch.setattr(va, "admit_bytes", _fake_admit_bytes)
    body = await _serve(repo_id="huggingface-hub", cmd="python -m pip install huggingface-hub")
    assert body["ok"] is True
    assert called == []
    assert "admission" not in body["receipt"]["plan"]
