"""HTTP-level tests for the new Diagnostics endpoints (COMUN.md rule 7: a
behaviour that crosses an HTTP route is tested through TestClient against the
real router, not through a mock of the route function).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.diagnostics_routes import setup_diagnostics_routes


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("routes.diagnostics_routes.require_admin", lambda request: None)
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None, None))
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    import src.settings as settings_module
    from src import safe_mode
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_module._invalidate_caches()
    safe_mode._active_cache = None
    safe_mode._reason_cache = ""
    yield


def test_doctor_repair_rejects_an_unknown_repair_over_http(client):
    resp = client.post("/api/doctor/repair", json={"repair": "curl | sh"})
    assert resp.status_code == 400


def test_doctor_repair_runs_an_allowlisted_repair_over_http(client, monkeypatch):
    from src import doctor
    monkeypatch.setitem(doctor.REPAIRS, "npm_ci", doctor.REPAIRS["npm_ci"])
    monkeypatch.setattr(doctor, "repair", lambda name, **kw: {"ok": True, "repair": name})
    resp = client.post("/api/doctor/repair", json={"repair": "npm_ci"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "repair": "npm_ci"}


def test_setup_status_surfaces_the_next_action_over_http(client, monkeypatch):
    from src import doctor
    monkeypatch.setattr(doctor, "next_setup_action", lambda: {
        "blocked": True, "finding": {"area": "setup", "name": "admin account"},
        "action": {"label": "Finish first-run setup", "route": "/setup"},
    })
    resp = client.get("/api/setup/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["blocked"] is True
    assert body["action"]["route"] == "/setup"


def test_safe_mode_status_over_http_reflects_the_module(client):
    from src import safe_mode
    safe_mode.activate("test via http")
    resp = client.get("/api/safe-mode/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["reason"] == "test via http"
    assert all(body["disabled"].values())


def test_safe_mode_reactivate_subsystem_over_http(client):
    from src import safe_mode
    safe_mode.activate("test")
    resp = client.post("/api/safe-mode/reactivate", json={"subsystem": "scheduled_tasks"})
    assert resp.status_code == 200
    assert resp.json()["disabled"]["scheduled_tasks"] is False


def test_safe_mode_reactivate_mcp_server_over_http(client):
    from src import safe_mode
    for _ in range(safe_mode.MCP_QUARANTINE_THRESHOLD):
        safe_mode.record_mcp_connection("flaky", "error")
    assert "flaky" in safe_mode.quarantined_servers()
    resp = client.post("/api/safe-mode/reactivate", json={"mcp_server_id": "flaky"})
    assert resp.status_code == 200
    assert resp.json()["quarantined"] is False
    assert "flaky" not in safe_mode.quarantined_servers()


def test_safe_mode_reactivate_requires_a_target_over_http(client):
    resp = client.post("/api/safe-mode/reactivate", json={})
    assert resp.status_code == 400


def test_provider_change_preview_reports_local_vs_cloud_and_requires_confirmation(client, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import core.database as database

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    with sessions() as db:
        db.add(database.ModelEndpoint(
            id="local-1", name="Local Ollama", base_url="http://127.0.0.1:11434/v1",
            endpoint_kind="local", is_enabled=True))
        db.add(database.ModelEndpoint(
            id="cloud-1", name="Some Cloud API", base_url="https://api.example.com/v1",
            endpoint_kind="api", api_key="sk-test", is_enabled=True))
        db.commit()
    try:
        resp = client.get("/api/setup/provider-change-preview",
                          params={"from_endpoint_id": "local-1", "to_endpoint_id": "cloud-1"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"]
        assert body["requires_confirmation"] is True
        assert "local" in body["before"]["privacy"]
        assert "cloud" in body["after"]["privacy"]
        assert body["changes"], "moving local -> cloud must be reported as a change"
    finally:
        engine.dispose()


def test_provider_change_preview_is_honest_about_an_unknown_endpoint(client, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import core.database as database

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database.ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessions)
    try:
        resp = client.get("/api/setup/provider-change-preview",
                          params={"from_endpoint_id": "nope", "to_endpoint_id": "also-nope"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["before"] is None and body["after"] is None
    finally:
        engine.dispose()
