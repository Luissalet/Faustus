"""L43 · TOOL-04 — the manifest routes in routes/mcp/mcp_routes.py, driven
through the real router with TestClient (COMUN.md rule 7): record/read a
per-server manifest, a permission diff that quarantines the server, and
explicit approval that lifts it.
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
def client(monkeypatch, tmp_path, db):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    manager = McpManager()
    app = FastAPI()
    app.include_router(mcp_routes.setup_mcp_routes(manager))
    yield TestClient(app, raise_server_exceptions=False)
    settings_mod._invalidate_caches()


def _add_server(db, server_id="srv1", command="npx", args=None):
    db.add(McpServer(id=server_id, name="Some MCP", transport="stdio", command=command,
                     args=json.dumps(args or ["-y", "some-server"]), env="{}", is_enabled=True))
    db.commit()


def test_no_manifest_recorded_yet_reads_as_not_installed(client, db):
    _add_server(db)
    r = client.get("/api/mcp/servers/srv1/manifest")
    assert r.status_code == 200, r.text
    assert r.json() == {"server_id": "srv1", "manifest": None, "installed": False}


def test_recording_a_manifest_reads_command_and_args_off_the_stored_server_row(client, db):
    _add_server(db, command="npx", args=["-y", "some-server"])
    r = client.post("/api/mcp/servers/srv1/manifest", json={
        "name": "Some MCP", "version": "1.0.0", "permissions": {"network": True},
        "dependencies": ["some-server"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["quarantined"] is False
    assert body["manifest"]["command_hash"] != ""
    assert body["manifest"]["permissions"] == {"network": True, "files": False, "secrets": False}

    read = client.get("/api/mcp/servers/srv1/manifest").json()
    assert read["installed"] is True
    assert read["manifest"]["version"] == "1.0.0"


def test_an_unknown_server_404s(client, db):
    r = client.post("/api/mcp/servers/nope/manifest", json={"permissions": {}})
    assert r.status_code == 404


def test_a_new_permission_on_update_quarantines_and_shows_on_the_server_list(client, db):
    _add_server(db)
    client.post("/api/mcp/servers/srv1/manifest", json={"permissions": {"network": True}})
    r = client.post("/api/mcp/servers/srv1/manifest", json={"permissions": {"network": True, "files": True}})
    assert r.status_code == 200
    assert r.json()["quarantined"] is True
    assert r.json()["diff"]["added"] == ["files"]

    servers = client.get("/api/mcp/servers").json()
    row = next(s for s in servers if s["id"] == "srv1")
    assert row["manifest_pending_approval"] is True


def test_approving_lifts_the_quarantine(client, db):
    _add_server(db)
    client.post("/api/mcp/servers/srv1/manifest", json={"permissions": {"network": True}})
    client.post("/api/mcp/servers/srv1/manifest", json={"permissions": {"network": True, "secrets": True}})
    assert client.get("/api/mcp/servers/srv1/manifest").json()["manifest"]["pending_approval"] is True

    r = client.post("/api/mcp/servers/srv1/manifest/approve")
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["pending_approval"] is False

    servers = client.get("/api/mcp/servers").json()
    row = next(s for s in servers if s["id"] == "srv1")
    assert row["manifest_pending_approval"] is False


def test_approving_an_unmanifested_server_404s(client, db):
    _add_server(db)
    r = client.post("/api/mcp/servers/srv1/manifest/approve")
    assert r.status_code == 404


def test_tools_page_route_reaches_the_manager(client, db, monkeypatch):
    _add_server(db)

    async def fake_page(self, server_id, cursor=None):
        assert server_id == "srv1"
        return {"tools": [{"name": "t1"}], "next_cursor": None, "paginated": False}

    monkeypatch.setattr("src.mcp_manager.McpManager.list_tools_page", fake_page)
    r = client.get("/api/mcp/servers/srv1/tools/page")
    assert r.status_code == 200, r.text
    assert r.json() == {"tools": [{"name": "t1"}], "next_cursor": None, "paginated": False}
