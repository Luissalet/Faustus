"""tests/test_prior_art_routes.py — HTTP surface for prior-art verification
(routes/prior_art_routes.py). Same TestClient-against-a-real-router pattern
`tests/test_tool_registry.py` uses: a standalone FastAPI app with only this
router mounted, `require_admin` stubbed out, and `src.prior_art` monkeypatched
so this file is only about the routes themselves.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.prior_art_routes as route_module


def _client(monkeypatch, current_user=""):
    monkeypatch.setattr(route_module, "require_admin", lambda request: None)

    app = FastAPI()

    @app.middleware("http")
    async def _stamp_user(request, call_next):
        request.state.current_user = current_user
        return await call_next(request)

    app.include_router(route_module.setup_prior_art_routes())
    return TestClient(app)


def test_rubric_route(monkeypatch):
    monkeypatch.setattr(route_module.prior_art, "rubric",
                        lambda idea, **kw: {"idea": idea, "instructions": "x", "slate_shape": {}})
    resp = _client(monkeypatch).post("/api/prior-art/rubric", json={"idea": "a pdf converter"})
    assert resp.status_code == 200
    assert resp.json()["idea"] == "a pdf converter"


def test_verify_route_passes_owner_from_request_state(monkeypatch):
    captured = {}

    def fake_verify(slate, **kw):
        captured["slate"] = slate
        captured["kw"] = kw
        return {"verified": True, "table": "", "components": [], "id": "PA-000001", "exit_code": 0}

    monkeypatch.setattr(route_module.prior_art, "verify", fake_verify)
    body = {"slate": {"idea": "x", "components": [{"name": "y", "verdict": "reuse", "repos": ["a/b"]}]},
           "target_license": "MIT"}
    resp = _client(monkeypatch, current_user="ada").post("/api/prior-art/verify", json=body)
    assert resp.status_code == 200
    assert resp.json()["id"] == "PA-000001"
    assert captured["kw"]["owner"] == "ada"
    assert captured["kw"]["target_license"] == "MIT"
    assert captured["slate"]["components"][0]["repos"] == ["a/b"]


def test_search_route(monkeypatch):
    monkeypatch.setattr(route_module.prior_art, "search",
                        lambda query, **kw: {"verified": True, "results": [{"full_name": "psf/requests"}]})
    resp = _client(monkeypatch).post("/api/prior-art/search", json={"query": "http client"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["full_name"] == "psf/requests"


def test_reports_listing_route(monkeypatch):
    monkeypatch.setattr(route_module.prior_art, "reports", lambda limit: [{"id": "PA-000001"}])
    resp = _client(monkeypatch).get("/api/prior-art/reports", params={"limit": 5})
    assert resp.status_code == 200
    assert resp.json() == {"reports": [{"id": "PA-000001"}]}


def test_report_by_id_route_found_and_404(monkeypatch):
    def fake_report(report_id):
        return {"id": report_id} if report_id == "PA-000001" else None

    monkeypatch.setattr(route_module.prior_art, "report", fake_report)
    client = _client(monkeypatch)

    found = client.get("/api/prior-art/reports/PA-000001")
    assert found.status_code == 200
    assert found.json()["id"] == "PA-000001"

    missing = client.get("/api/prior-art/reports/PA-999999")
    assert missing.status_code == 404


def test_a_valueerror_from_the_module_is_a_400(monkeypatch):
    def boom(idea, **kw):
        raise ValueError("bad idea")

    monkeypatch.setattr(route_module.prior_art, "rubric", boom)
    resp = _client(monkeypatch).post("/api/prior-art/rubric", json={"idea": "x"})
    assert resp.status_code == 400
