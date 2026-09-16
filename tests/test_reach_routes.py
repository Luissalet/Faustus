"""/api/reach/* routes (R1) — admin-gated, wraps src/reach/router.py + doctor.py."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.middleware as mw
from routes.reach_routes import setup_reach_routes


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(mw, "auth_disabled", lambda: True)
    app = FastAPI()
    app.include_router(setup_reach_routes())
    return TestClient(app)


def test_doctor_route_returns_summary(monkeypatch):
    client = _client(monkeypatch)
    resp = client.get("/api/reach/doctor")
    assert resp.status_code == 200
    body = resp.json()
    assert "channels" in body
    assert body["live"] is False


def test_read_route_requires_url(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/reach/read", json={"url": ""})
    assert resp.status_code == 400
    assert resp.json()["error_class"] == "invalid_request"


def test_read_route_calls_router_and_returns_result_dict(monkeypatch):
    client = _client(monkeypatch)

    async def fake_read(url, channel=None, **kw):
        from src.reach.base import ReachResult
        return ReachResult(channel="web", backend="jina_reader", url=url, text="hello")

    import src.reach.router as reach_router
    monkeypatch.setattr(reach_router, "read", fake_read)

    resp = client.post("/api/reach/read", json={"url": "example.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "hello"
    assert body["backend"] == "jina_reader"


def test_search_route_requires_query(monkeypatch):
    client = _client(monkeypatch)
    resp = client.post("/api/reach/search", json={"query": ""})
    assert resp.status_code == 400
