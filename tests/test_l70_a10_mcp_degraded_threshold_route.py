"""Lote 70a, punto A.10 — GET/PUT /api/mcp/servers/{server_id}/degraded-threshold.

`src/mcp_manager.py::McpManager.degraded_threshold_for`/`set_degraded_threshold`
(TOOL-03) already existed with no route reaching them: an admin had no way to
see or change a server's degradation thresholds short of hand-editing
`data/settings.json`, and `set_degraded_threshold`'s in-process override
never survived a restart on its own. This route is that surface — same
TestClient-through-the-real-router pattern
`tests/test_l43_tool04_manifest_routes.py` already uses for this file.
"""
from __future__ import annotations

import json

import pytest
import sqlalchemy
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker
from starlette.testclient import TestClient

import routes.mcp.mcp_routes as mcp_routes
from core.database import McpServer
from src import settings as settings_mod
from src.mcp_manager import McpManager


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_routes, "SessionLocal", Session)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def manager():
    return McpManager()


@pytest.fixture
def client(monkeypatch, tmp_path, db, manager):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    app = FastAPI()
    app.include_router(mcp_routes.setup_mcp_routes(manager))
    yield TestClient(app, raise_server_exceptions=False)
    settings_mod._invalidate_caches()


def _add_server(db, server_id="srv1"):
    db.add(McpServer(id=server_id, name="Some MCP", transport="stdio", command="npx",
                     args="[]", env="{}", is_enabled=True))
    db.commit()


def test_get_on_an_unconfigured_server_reads_the_class_defaults(client, db, manager):
    _add_server(db)
    r = client.get("/api/mcp/servers/srv1/degraded-threshold")
    assert r.status_code == 200
    assert r.json() == {"id": "srv1", "thresholds": manager.degraded_threshold_for("srv1")}
    assert r.json()["thresholds"]["error_threshold"] == manager._DEGRADED_ERROR_THRESHOLD


def test_get_on_an_unknown_server_404s(client, db):
    r = client.get("/api/mcp/servers/nope/degraded-threshold")
    assert r.status_code == 404


def test_put_persists_to_the_setting_and_applies_immediately(client, db, manager):
    _add_server(db)
    r = client.put("/api/mcp/servers/srv1/degraded-threshold",
                   json={"error_threshold": 0.9, "latency_threshold_s": 30})
    assert r.status_code == 200
    body = r.json()
    assert body["thresholds"]["error_threshold"] == 0.9
    assert body["thresholds"]["latency_threshold_s"] == 30.0
    # Fields not sent are untouched (class default carried through).
    assert body["thresholds"]["error_window_s"] == manager._DEGRADED_ERROR_WINDOW_S

    # Applied to THIS process immediately (TOOL-03's own point — an
    # in-process override that survives without needing a restart to apply).
    assert manager.degraded_threshold_for("srv1")["error_threshold"] == 0.9

    # And persisted where a restart would read it back from.
    persisted = settings_mod.get_setting("mcp_degraded_thresholds", {})
    assert persisted["srv1"] == {"error_threshold": 0.9, "latency_threshold_s": 30.0}


def test_put_survives_a_fresh_manager_reading_from_the_persisted_setting(client, db, manager):
    """The actual "restart" proof: a brand-new McpManager (no in-process
    override of its own) still sees the threshold, because it was written
    to the persisted setting, not only held in memory."""
    _add_server(db)
    client.put("/api/mcp/servers/srv1/degraded-threshold", json={"latency_samples": 7})

    fresh = McpManager()
    assert fresh.degraded_threshold_for("srv1")["latency_samples"] == 7.0


def test_put_merges_rather_than_replacing_earlier_fields(client, db, manager):
    _add_server(db)
    client.put("/api/mcp/servers/srv1/degraded-threshold", json={"error_threshold": 0.5})
    r = client.put("/api/mcp/servers/srv1/degraded-threshold", json={"latency_samples": 5})
    assert r.status_code == 200
    thresholds = r.json()["thresholds"]
    assert thresholds["error_threshold"] == 0.5
    assert thresholds["latency_samples"] == 5.0
    persisted = settings_mod.get_setting("mcp_degraded_thresholds", {})
    assert persisted["srv1"] == {"error_threshold": 0.5, "latency_samples": 5.0}


def test_put_rejects_an_unknown_field_only_body(client, db):
    _add_server(db)
    r = client.put("/api/mcp/servers/srv1/degraded-threshold", json={"not_a_real_field": 1})
    assert r.status_code == 400


def test_put_rejects_a_non_numeric_value(client, db):
    _add_server(db)
    r = client.put("/api/mcp/servers/srv1/degraded-threshold", json={"error_threshold": "high"})
    assert r.status_code == 400


def test_put_on_an_unknown_server_404s(client, db):
    r = client.put("/api/mcp/servers/nope/degraded-threshold", json={"error_threshold": 0.5})
    assert r.status_code == 404


def test_a_different_servers_thresholds_are_untouched(client, db, manager):
    _add_server(db, "srv1")
    _add_server(db, "srv2")
    client.put("/api/mcp/servers/srv1/degraded-threshold", json={"error_threshold": 0.9})
    r2 = client.get("/api/mcp/servers/srv2/degraded-threshold")
    assert r2.json()["thresholds"]["error_threshold"] == manager._DEGRADED_ERROR_THRESHOLD
