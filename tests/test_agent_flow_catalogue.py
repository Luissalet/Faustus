from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.skill_library_routes import setup_skill_library_routes
from src import hoard_hub


def test_flows_proxy_rewrites_source_links_and_keeps_source(monkeypatch):
    calls = []

    def call(method, path, **kwargs):
        calls.append((method, path))
        return 200, {"ok": True, "apps": [{"id": "darwin", "shelf_url": "/api/skills/page?app=darwin", "flows": [
            {"skill": "discovery", "steps": "1. Read.\n2. Compare.", "skill_url": "/api/skills/app/darwin/read/discovery"}]}]}, {"url": "http://hub"}

    monkeypatch.setattr(hoard_hub, "_call", call)
    app = FastAPI()
    app.include_router(setup_skill_library_routes())
    result = TestClient(app).get("/api/skills/library/agent-flows?app=darwin&q=hello+world").json()
    row = result["apps"][0]
    assert row["shelf_url"] == "http://hub/api/skills/page?app=darwin"
    assert row["flows"][0]["steps"] == "1. Read.\n2. Compare."
    assert row["flows"][0]["skill_url"] == "/api/skills/library/agent-flow-skill?app=darwin&slug=discovery"
    assert calls == [("GET", "/api/skills/flows?app=darwin&q=hello+world")]


def test_flows_proxy_reports_hub_down(monkeypatch):
    monkeypatch.setattr(hoard_hub, "_call", lambda *a, **k: (None, None, {}))
    app = FastAPI()
    app.include_router(setup_skill_library_routes())
    client = TestClient(app)
    assert client.get("/api/skills/library/agent-flows").json()["ok"] is False
    assert client.get("/api/skills/library/agent-flow-skill?app=darwin&slug=discovery").json()["ok"] is False
