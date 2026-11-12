import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import privacy_routes
from src import data_boundary_report as boundary


@pytest.mark.parametrize(("url", "kind", "address", "scope"), [
    ("https://user:password@api.example.test:443/path?token=secret#fragment", "api", "api.example.test:443", "external"),
    ("http://[::1]:8000/v1", "local", "[::1]:8000", "machine"),
    ("http://127.0.0.1:8000/v1", "proxy", "127.0.0.1:8000", "unknown"),
    ("http://192.168.1.8:8000/v1", "local", "192.168.1.8:8000", "network"),
    ("http://100.65.1.2", "auto", "100.65.1.2", "network"),
    ("http://spark.tailnet.ts.net", "local", "spark.tailnet.ts.net", "network"),
    ("https://host:invalid", "auto", "", "unknown"),
    ("", "auto", "", "unknown"),
])
def test_destination_classification(url, kind, address, scope):
    assert boundary.destination(url, kind) == {"address": address, "scope": scope}


def test_report_only_returns_routing_metadata():
    base = "https://user:password@models.example.test/private-route?token=secret"
    snapshot = {
        "endpoints": [{"id": "remote", "name": "Remote", "base_url": base,
                       "cached_models": '["demo"]', "api_key": "model-key"}],
        "sessions": [{"id": "chat", "name": "Chat", "model": "demo", "endpoint_url": base,
                      "project_id": "project", "messages": ["private message"]},
                     {"id": "legacy", "name": "Legacy", "model": "old",
                      "endpoint_url": "http://spark.local:8000/v1", "folder": "Example"}],
        "projects": [{"id": "project", "name": "Example project", "folder": "Example",
                      "instructions": "private instructions"}],
        "connectors": [{"id": "connector", "name": "Connector", "transport": "stdio",
                        "command": "secret command", "env": {"KEY": "secret env"}}],
        "emails": [{"id": "mail", "name": "Mail", "smtp_host": "smtp.example.test",
                    "smtp_password": "mail password", "enabled": False}],
        "prefs": {"_users": {"private owner": {"default_endpoint_id": "remote", "caldav": None,
                  "google_calendar_accounts": [{"refresh_token": "calendar token", "status": "needs_reauth"}]}}},
        "settings": {"search_provider": "disabled", "search_fallback_chain": ["duckduckgo"],
                     "research_search_provider": "brave", "default_endpoint_id": "remote"},
    }
    result = boundary.build_report(snapshot)
    routes = {row["id"]: row for row in result["routes"]}
    assert routes["remote"]["address"] == "models.example.test"
    assert routes["remote"]["roles"] == ["default"]
    assert routes["remote"]["user_override_count"] == 1
    assert routes["remote"]["conversations"][0]["project_name"] == "Example project"
    assert routes["session:legacy"]["conversations"][0]["project_id"] == "project"
    assert routes["connector"]["scope"] == "unknown"
    assert routes["mail:smtp_host"]["enabled"] is False
    assert routes["calendar:0:0"]["enabled"] is False
    assert routes["search:brave"]["enabled"] is True
    assert routes["search:duckduckgo"]["enabled"] is False
    payload = json.dumps(result)
    for private in ("private-route", "password", "secret", "model-key", "private message",
                    "private instructions", "private owner", "calendar token", '"folder"'):
        assert private not in payload


def test_malformed_metadata_is_reported_without_rewriting(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")
    missing = []
    assert boundary._json_metadata(path, {}, missing) == {}
    assert missing == ["settings.json"]
    assert path.read_text(encoding="utf-8") == "[]"


def test_report_reuses_admin_gate_and_disables_caching(monkeypatch):
    from fastapi import HTTPException

    calls = []
    def admin(request):
        if request.headers.get("x-test-admin") != "yes":
            raise HTTPException(403)

    monkeypatch.setattr(privacy_routes, "require_admin", admin)
    monkeypatch.setattr(boundary, "report", lambda: calls.append(True) or {"version": 1})
    app = FastAPI()
    app.include_router(privacy_routes.setup_privacy_routes())
    with TestClient(app) as client:
        assert client.get("/api/privacy/report").status_code == 403
        assert not calls
        response = client.get("/api/privacy/report", headers={"x-test-admin": "yes"})
        assert response.json() == {"version": 1}
        assert response.headers["cache-control"] == "no-store"
        assert calls == [True]
