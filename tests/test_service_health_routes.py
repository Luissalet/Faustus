"""The service-health endpoints (FAUSTUS).

Covers the two things the panel depends on: the GET report comes back with
hints attached, and the POST actually runs recovery before re-probing (a
Reconnect button that only re-reads the old state would be a lie).

`collect_service_health` is stubbed — the real one makes network probes, and
these are supposed to run in a suite, not on a network.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.diagnostics_routes as diag


class Store:
    def __init__(self, healthy=False):
        self.healthy = healthy
        self.reconnects = 0

    def reconnect(self):
        self.reconnects += 1
        self.healthy = True
        return True


@pytest.fixture
def stores():
    return Store(), Store()


@pytest.fixture
def client(monkeypatch, stores):
    monkeypatch.setattr(diag, "require_admin", lambda request: None)

    async def fake_collect(rag_manager=None, memory_vector=None):
        return {
            "overall": "down",
            "timestamp": "2026-08-31T00:00:00+00:00",
            "services": [
                {"name": "chromadb", "status": "down", "detail": "gone",
                 "meta": {"rag": False, "memory": False}},
                {"name": "ntfy", "status": "disabled", "detail": "off", "meta": {}},
            ],
        }

    import src.service_health as sh
    monkeypatch.setattr(sh, "collect_service_health", fake_collect)

    app = FastAPI()
    rag, mem = stores
    app.include_router(diag.setup_diagnostics_routes(rag, True, None, mem))
    return TestClient(app)


def test_report_carries_hints_for_failures_only(client):
    body = client.get("/api/diagnostics/services").json()
    chroma, ntfy = body["services"]
    assert "docker start" in chroma["hint"]["command"]
    assert "hint" not in ntfy


def test_reconnect_recovers_the_stores_then_reprobes(client, stores):
    rag, mem = stores
    body = client.post("/api/diagnostics/services/reconnect").json()
    assert (rag.reconnects, mem.reconnects) == (1, 1)
    assert body["recovery"] == {"chroma_client": "reset",
                                "rag": "healthy", "memory": "healthy"}
    assert body["services"][0]["hint"]["text"]


def test_both_endpoints_are_admin_only(monkeypatch, stores):
    """The report names configured endpoints and accounts — not for guests."""
    calls = []

    def deny(request):
        calls.append(request.url.path)
        raise PermissionError("nope")

    monkeypatch.setattr(diag, "require_admin", deny)
    app = FastAPI()
    app.include_router(diag.setup_diagnostics_routes(
        stores[0], True, None, stores[1]))
    client = TestClient(app, raise_server_exceptions=False)
    client.get("/api/diagnostics/services")
    client.post("/api/diagnostics/services/reconnect")
    assert calls == ["/api/diagnostics/services",
                     "/api/diagnostics/services/reconnect"]
