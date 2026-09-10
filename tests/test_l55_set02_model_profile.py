"""Lote 55 item 4 — SET-02: `GET /api/models/endpoint-profile`
(routes/local_models_routes.py) — the cost/privacy half of the model
picker's informative badges. Capabilities (`/api/models/{name}/capabilities`)
and VRAM fit (`/api/models/fit`) already existed and are already tested
elsewhere (tests/test_local_models_routes.py, tests/test_model_calibration.py);
this file covers only the new endpoint, against the real router with a fake
DB session — same `_Db`/`_Q` shape
`test_local_models_routes.py::test_endpoint_visibility_follows_ownership_like_api_models`
already uses for exactly this "owner sees own + shared, admin sees all" rule.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.testclient import TestClient

import routes.local_models_routes as lm

ROWS = [
    SimpleNamespace(id="shared-local", name="Ollama", base_url="http://localhost:11434/v1",
                    is_enabled=True, owner=None, api_key=None),
    SimpleNamespace(id="alices-cloud-keyed", name="Alice's OpenAI", base_url="https://api.openai.com/v1",
                    is_enabled=True, owner="alice", api_key="sk-configured"),
    SimpleNamespace(id="bobs-cloud-unkeyed", name="Bob's endpoint", base_url="https://example.com/v1",
                    is_enabled=True, owner="bob", api_key=None),
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
        return _Q(ROWS)

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(lm, "SessionLocal", lambda: _Db())
    monkeypatch.setattr(lm, "owner_filter", lambda q, model_cls, user, **kw: _Q([r for r in ROWS if r.owner in (None, user)]))

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


def test_requires_a_signed_in_user(client):
    assert client.get("/api/models/endpoint-profile").status_code == 401


def test_privacy_reads_the_base_url_not_a_declared_label(client):
    """A `localhost`/loopback base URL is `is_local` regardless of anything
    an admin might have labelled the endpoint — `privacy_policy.
    is_local_destination` is the only authority consulted, never a stored
    `endpoint_kind` string (see the route's own docstring for why)."""
    r = client.get("/api/models/endpoint-profile", headers={"x-user": "alice"})
    assert r.status_code == 200
    profiles = r.json()["endpoints"]
    assert profiles["shared-local"] == {"is_local": True, "cost": "free_local", "has_api_key": False}


def test_cost_distinguishes_a_keyed_cloud_endpoint_from_an_unconfigured_one(client):
    r = client.get("/api/models/endpoint-profile", headers={"x-user": "alice"})
    profiles = r.json()["endpoints"]
    assert profiles["alices-cloud-keyed"] == {"is_local": False, "cost": "paid", "has_api_key": True}


def test_owner_scoping_matches_get_api_models(client):
    """alice sees the shared endpoint and her own, never bob's; root (admin)
    sees everything — the identical rule GET /api/models itself applies."""
    as_alice = client.get("/api/models/endpoint-profile", headers={"x-user": "alice"}).json()["endpoints"]
    assert set(as_alice) == {"shared-local", "alices-cloud-keyed"}
    assert "bobs-cloud-unkeyed" not in as_alice

    as_root = client.get("/api/models/endpoint-profile", headers={"x-user": "root"}).json()["endpoints"]
    assert set(as_root) == {"shared-local", "alices-cloud-keyed", "bobs-cloud-unkeyed"}
    assert as_root["bobs-cloud-unkeyed"] == {"is_local": False, "cost": "unconfigured", "has_api_key": False}
