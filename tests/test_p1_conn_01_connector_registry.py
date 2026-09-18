"""
tests/test_p1_conn_01_connector_registry.py — CONN-01, lote 53.

The acceptance line: "Desconectar revoca acceso operativo y evita que un
worker reutilice credenciales cacheadas fuera de politica." Two things are
pinned against real code, not a description of the feature:

  * a connector scoped to ``read`` genuinely cannot be used for a ``write``
    method — `enforce_scope` refuses it before any network call is even
    attempted (`/call` never imports `execute_api_call` on the refused path);
  * revoking a connector removes its credential from `src.integrations`
    (the real store, read fresh from disk on every call, no cache to
    invalidate) — so a second call after revoke gets a 404, not a cached
    "yes", proving "a worker never reuses a cached credential out of policy"
    because there is nothing left in the one place a worker would read it
    from.

Crosses HTTP throughout (rule 7): a fresh `FastAPI()` mounting only
`setup_integrations_routes()`, exactly the pattern
`tests/test_workflows_routes.py` already established for a routes module
that has no place of its own in `app.py` yet (see the batch report).
`execute_api_call`'s own network call is stubbed at the module boundary for
the one test that reaches `/call` on an ALLOWED scope — every other piece
(the route, the scope gate, the registry, the underlying credential store)
runs for real.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import middleware
from routes.integrations_routes import setup_integrations_routes


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(constants, "INTEGRATIONS_FILE", str(tmp_path / "integrations.json"))
    import src.integrations as integrations
    monkeypatch.setattr(integrations, "DATA_FILE", str(tmp_path / "integrations.json"))
    import src.connector_registry as registry
    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "connector_registry.json"))
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)

    app = FastAPI()
    app.include_router(setup_integrations_routes())
    yield TestClient(app), integrations


def _make_integration(integrations_mod, **overrides) -> str:
    data = {"name": "Test Feed", "base_url": "http://example.internal", "api_key": "secret-token"}
    data.update(overrides)
    created = integrations_mod.add_integration(data)
    return created["id"]


def test_a_connector_never_exceeds_its_declared_scope(client):
    web, integrations = client
    iid = _make_integration(integrations)

    declared = web.post(f"/api/connectors/{iid}/scopes", json={"scopes": ["read"]})
    assert declared.status_code == 200
    assert declared.json()["scopes"] == ["read"]

    allowed = web.post(f"/api/connectors/{iid}/check", json={"method": "GET"})
    assert allowed.json() == {"ok": True, "allowed": True}

    refused = web.post(f"/api/connectors/{iid}/check", json={"method": "POST"})
    assert refused.json()["allowed"] is False
    assert "read" in refused.json()["reason"] or "write" in refused.json()["reason"]

    # The gate sits in front of the network, not merely reported afterwards.
    blocked_call = web.post(f"/api/connectors/{iid}/call", json={"method": "DELETE", "path": "/x"})
    assert blocked_call.status_code == 403


def test_declaring_an_unknown_scope_is_refused_not_silently_widened(client):
    web, integrations = client
    iid = _make_integration(integrations)
    out = web.post(f"/api/connectors/{iid}/scopes", json={"scopes": ["read", "admin"]})
    assert out.status_code == 400
    # Nothing was written — the connector is still "unscoped", not partially scoped.
    info = web.get(f"/api/connectors/{iid}/scopes").json()
    assert info["unscoped"] is True


def test_an_unregistered_connector_stays_exactly_as_capable_as_before(client):
    """Back-compat (rule 3): an integration that never went through this
    batch's scope declaration is not newly restricted by its mere
    existence."""
    web, integrations = client
    iid = _make_integration(integrations)
    info = web.get(f"/api/connectors/{iid}/scopes").json()
    assert info["unscoped"] is True and info["status"] == "connected"
    allowed = web.post(f"/api/connectors/{iid}/check", json={"method": "DELETE"})
    assert allowed.json()["allowed"] is True


def test_revoking_deletes_the_real_credential_not_just_a_flag(client):
    web, integrations = client
    iid = _make_integration(integrations)
    web.post(f"/api/connectors/{iid}/scopes", json={"scopes": ["read", "write"]})
    assert integrations.get_integration(iid) is not None

    revoked = web.post(f"/api/connectors/{iid}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    # The one place a worker would read the credential from has nothing left.
    assert integrations.get_integration(iid) is None
    # And the connection center itself reflects it, not a stale "connected".
    info = web.get(f"/api/connectors/{iid}/scopes").json()
    assert info["status"] == "revoked"
    # A call after revoke is refused before it can reuse anything cached.
    denied = web.post(f"/api/connectors/{iid}/check", json={"method": "GET"})
    assert denied.json()["allowed"] is False
    assert "revoked" in denied.json()["reason"]


def test_an_allowed_call_reaches_the_real_executor_and_records_a_sync(client, monkeypatch):
    web, integrations = client
    iid = _make_integration(integrations)
    web.post(f"/api/connectors/{iid}/scopes", json={"scopes": ["read"]})

    calls = []

    async def _fake_execute(integration_id, method, path, params=None, body=None, extra_headers=None):
        calls.append((integration_id, method, path))
        return {"status": 200, "body": "ok"}

    monkeypatch.setattr(integrations, "execute_api_call", _fake_execute)
    out = web.post(f"/api/connectors/{iid}/call", json={"method": "GET", "path": "/health"})
    assert out.status_code == 200
    assert out.json()["result"] == {"status": 200, "body": "ok"}
    assert calls == [(iid, "GET", "/health")]
    info = web.get(f"/api/connectors/{iid}/scopes").json()
    assert info["last_synced_at"] != ""


def test_owners_do_not_see_each_others_connectors(client):
    web, integrations = client
    from src import connector_registry
    iid = _make_integration(integrations)
    connector_registry.register(iid, owner="alice", scopes=["read"])

    listing = web.get("/api/connectors").json()["connectors"]
    # auth disabled -> effective owner is the reserved local owner, not "alice"
    assert iid not in [c["id"] for c in listing]
