"""F1: the connector catalogue, sidecar, honest status, and the
`/api/app-connectors` routes (src/connectors.py, src/connector_sidecar.py,
src/connector_status.py, routes/connector_routes.py).

Route handlers are driven directly, the way `tests/test_mcp_routes_env_mode.py`
and `tests/test_l63_tool04_sec08_mcp_governance.py` already do — a TestClient
portal does not survive these async handlers reliably under this repo's
pytest-asyncio auto mode.

Fixtures never touch a real Jobhunter/Writer install (COMUN rule 8): the
"bridge script" is a throwaway file this test tree creates itself, and the
"app" is a real `http.server` on an ephemeral port, not the real thing.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import threading
import types

import pytest

import routes.connector_routes as connector_routes
import routes.mcp.mcp_routes as mcp_routes
import src.connector_sidecar as connector_sidecar
import src.connector_status as connector_status
import src.launch_profiles as launch_profiles
import src.settings as settings_mod
from core.database import McpServer
from src import connectors, security_policy
from src.mcp_manager import McpManager

pytestmark = pytest.mark.asyncio


# ── a fake, real-enough domain-app health server ──────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    status_code = 200
    body = b"{}"

    def do_GET(self):  # noqa: N802 - stdlib signature
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):  # noqa: D401 - silence stdout during tests
        pass


@pytest.fixture
def health_server():
    started = []

    def _make(status_code=200, body=b"{}"):
        handler = type("Handler", (_Handler,), {"status_code": status_code, "body": body})
        httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        started.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _make
    for httpd in started:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def bridge_dir(tmp_path):
    """A fake `{JOBHUNT_DIR}` with a real (but inert) `server/mcp.js`."""
    d = tmp_path / "jobhunt"
    (d / "server").mkdir(parents=True)
    (d / "server" / "mcp.js").write_text("// fake bridge\n")
    return str(d)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """No test may write into the real repo's data/ directory."""
    monkeypatch.setattr(connector_sidecar, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(launch_profiles, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod._invalidate_caches()
    monkeypatch.setattr(security_policy, "AUDIT_DATA_DIR", str(tmp_path / "sec08"))
    security_policy.consent_store._records.clear()
    security_policy.clear_audit_log()
    connector_status._health_cache.clear()
    yield
    connector_status._health_cache.clear()
    security_policy.consent_store._records.clear()
    security_policy.clear_audit_log()
    settings_mod._invalidate_caches()


@pytest.fixture
def db(tmp_path, monkeypatch):
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(mcp_routes, "SessionLocal", Session)
    monkeypatch.setattr(connector_routes, "SessionLocal", Session)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def manager(monkeypatch):
    """A McpManager whose connect/disconnect only record what they were asked
    (the same fake `tests/test_l63_tool04_sec08_mcp_governance.py` uses) —
    tool_count is settable per test via `manager.tool_count`."""
    mgr = McpManager()
    mgr.tool_count = 3
    calls = {"connect": [], "disconnect": []}

    async def fake_connect(server_id, name, transport, command=None, args=None,
                           env=None, url=None, inherit_env=None):
        calls["connect"].append(server_id)
        mgr._connections[server_id] = {
            "status": "connected", "name": name, "transport": transport,
            "tool_count": mgr.tool_count, "error": None,
        }
        return True

    async def fake_disconnect(server_id):
        calls["disconnect"].append(server_id)
        mgr._connections.pop(server_id, None)

    monkeypatch.setattr(mgr, "connect_server", fake_connect)
    monkeypatch.setattr(mgr, "disconnect_server", fake_disconnect)
    mgr.calls = calls
    return mgr


@pytest.fixture
def routes(monkeypatch, manager):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(connector_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(connector_routes, "require_human", lambda request: None)
    router = connector_routes.setup_connector_routes(manager)
    by_path = {}
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            by_path[(method, route.path)] = route.endpoint
    return by_path


class FakeRequest:
    def __init__(self, body=None, user="tester", client_host="127.0.0.1"):
        self._body = body if body is not None else {}
        self.state = types.SimpleNamespace(current_user=user)
        self.client = types.SimpleNamespace(host=client_host) if client_host else None
        self.headers = {}

    async def json(self):
        return self._body


def _call(fn, request=None, **kwargs):
    result = fn(request=request or FakeRequest(), **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


# ── F1.1: preset resolution ────────────────────────────────────────────────

def test_missing_placeholder_is_unconfigured():
    preset = connectors.get_preset("jobhunter")
    resolved = connectors.resolve_preset_values(preset, {"APP_URL": "http://127.0.0.1:5178"})
    assert resolved["ok"] is False
    assert "JOBHUNT_DIR" in resolved["missing"]


def test_missing_bridge_file_is_unconfigured(tmp_path):
    preset = connectors.get_preset("jobhunter")
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    resolved = connectors.resolve_preset_values(
        preset, {"JOBHUNT_DIR": str(empty_dir), "APP_URL": "http://127.0.0.1:5178"})
    assert resolved["ok"] is False
    assert any("bridge script not found" in r for r in resolved["reasons"])


def test_a_fully_configured_preset_resolves(bridge_dir):
    preset = connectors.get_preset("jobhunter")
    resolved = connectors.resolve_preset_values(
        preset, {"JOBHUNT_DIR": bridge_dir, "APP_URL": "http://127.0.0.1:5178"})
    assert resolved["ok"] is True
    assert resolved["args"] == [f"{bridge_dir}/server/mcp.js"]
    assert resolved["env"]["JOBHUNT_URL"] == "http://127.0.0.1:5178"
    assert resolved["ui_url"] == "http://127.0.0.1:5178"


# ── F1.3: honest status ────────────────────────────────────────────────────

async def test_health_rejected_is_app_off_even_if_adapter_says_connected(bridge_dir):
    preset = connectors.get_preset("jobhunter")
    values = {"JOBHUNT_DIR": bridge_dir, "APP_URL": "http://127.0.0.1:1"}  # nothing listens here
    manager_status = {"status": "connected", "tool_count": 5, "error": None}
    status = await connector_status.compute_status(
        preset, values=values, is_enabled=True, connector_id="c1",
        manager_status=manager_status, force_check=True,
    )
    assert status["state"] == "app_off"
    assert status["app"]["reachable"] is False


async def test_writer_wrong_service_is_error(bridge_dir, health_server):
    base_url = health_server(200, json.dumps({"service": "some-other-app"}).encode())
    preset = connectors.get_preset("writer")
    values = {"WRITER_DIR": bridge_dir, "APP_URL": base_url, "TOKEN_FILE": str(__import__("pathlib").Path(bridge_dir) / "token")}
    # Writer's bridge script path differs from Jobhunter's; make it exist too.
    import os
    writer_bridge = os.path.join(bridge_dir, "dist-electron", "aibridge")
    os.makedirs(writer_bridge, exist_ok=True)
    open(os.path.join(writer_bridge, "mcpStdio.cjs"), "w").close()

    manager_status = {"status": "connected", "tool_count": 2, "error": None}
    status = await connector_status.compute_status(
        preset, values=values, is_enabled=True, connector_id="c2",
        manager_status=manager_status, force_check=True,
    )
    assert status["state"] == "error"
    assert any("service='some-other-app'" in r or "some-other-app" in r for r in status["reasons"])


async def test_health_ok_and_connected_is_available(bridge_dir, health_server):
    base_url = health_server(200, b"{}")
    preset = connectors.get_preset("jobhunter")
    values = {"JOBHUNT_DIR": bridge_dir, "APP_URL": base_url}
    manager_status = {"status": "connected", "tool_count": 4, "error": None}
    status = await connector_status.compute_status(
        preset, values=values, is_enabled=True, connector_id="c3",
        manager_status=manager_status, force_check=True,
    )
    assert status["state"] == "available"
    assert status["adapter"]["tool_count"] == 4


async def test_disabled_wins_over_everything(bridge_dir):
    preset = connectors.get_preset("jobhunter")
    status = await connector_status.compute_status(
        preset, values={"JOBHUNT_DIR": bridge_dir, "APP_URL": "http://127.0.0.1:5178"},
        is_enabled=False, connector_id="c4",
        manager_status={"status": "connected", "tool_count": 9}, force_check=True,
    )
    assert status["state"] == "disabled"


async def test_never_checked_is_unknown_not_false(bridge_dir):
    preset = connectors.get_preset("jobhunter")
    status = await connector_status.compute_status(
        preset, values={"JOBHUNT_DIR": bridge_dir, "APP_URL": "http://127.0.0.1:59999"},
        is_enabled=True, connector_id="c5-never-checked",
        manager_status={"status": "disconnected"}, force_check=False,
    )
    assert status["state"] == "unknown"
    assert status["app"]["reachable"] is None


# ── F1.2: redaction ─────────────────────────────────────────────────────────

def test_secret_shaped_values_are_redacted_on_read():
    entry = connector_sidecar.create_connector(
        preset_id="jobhunter", server_id="srv1", owner="tester",
        values={"JOBHUNT_DIR": "/x", "APP_URL": "http://127.0.0.1:5178", "SOME_TOKEN": "hunter2"},
        app_url="http://127.0.0.1:5178", ui_url="http://127.0.0.1:5178",
    )
    read_back = connector_sidecar.get_connector(entry["id"], redact=True)
    assert read_back["values"]["SOME_TOKEN"] == "***redacted***"
    assert read_back["values"]["JOBHUNT_DIR"] == "/x"
    unredacted = connector_sidecar.get_connector(entry["id"], redact=False)
    assert unredacted["values"]["SOME_TOKEN"] == "hunter2"


# ── F1.5: routes ─────────────────────────────────────────────────────────

async def test_create_connector_then_duplicate_is_409(routes, db, bridge_dir):
    body = {"preset_id": "jobhunter", "values": {"JOBHUNT_DIR": bridge_dir,
                                                  "APP_URL": "http://127.0.0.1:5178"}}
    first = await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    assert first["preset_id"] == "jobhunter"
    assert first["values"]["JOBHUNT_DIR"] == bridge_dir

    with pytest.raises(Exception) as exc_info:
        await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    from fastapi import HTTPException
    assert isinstance(exc_info.value, HTTPException)
    assert exc_info.value.status_code == 409


async def test_patch_values_regenerates_args_and_disconnects(routes, db, manager, bridge_dir, tmp_path):
    body = {"preset_id": "jobhunter", "values": {"JOBHUNT_DIR": bridge_dir,
                                                  "APP_URL": "http://127.0.0.1:5178"}}
    created = await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    server_id = created["server"]["id"]
    await manager.connect_server(server_id=server_id, name="x", transport="stdio", command="node", args=[])
    assert server_id in manager._connections

    new_dir = tmp_path / "jobhunt2"
    (new_dir / "server").mkdir(parents=True)
    (new_dir / "server" / "mcp.js").write_text("// fake\n")
    patch_body = {"values": {"JOBHUNT_DIR": str(new_dir)}}
    updated = await routes[("PATCH", "/api/app-connectors/{connector_id}")](
        connector_id=created["id"], request=FakeRequest(body=patch_body))

    assert updated["values"]["JOBHUNT_DIR"] == str(new_dir)
    row = db.query(McpServer).filter(McpServer.id == server_id).first()
    assert json.loads(row.args) == [f"{new_dir}/server/mcp.js"]
    assert server_id in manager.calls["disconnect"]


async def test_disabled_connector_state(routes, db, bridge_dir):
    body = {"preset_id": "jobhunter", "values": {"JOBHUNT_DIR": bridge_dir,
                                                  "APP_URL": "http://127.0.0.1:5178"}}
    created = await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    updated = await routes[("PATCH", "/api/app-connectors/{connector_id}")](
        connector_id=created["id"], request=FakeRequest(body={"is_enabled": False}))
    assert updated["status"]["state"] == "disabled"
    assert updated["server"]["is_enabled"] is False


async def test_delete_connector_removes_sidecar_and_server(routes, db, bridge_dir):
    body = {"preset_id": "jobhunter", "values": {"JOBHUNT_DIR": bridge_dir,
                                                  "APP_URL": "http://127.0.0.1:5178"}}
    created = await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    result = await routes[("DELETE", "/api/app-connectors/{connector_id}")](
        connector_id=created["id"], request=FakeRequest())
    assert result == {"status": "deleted"}
    assert connector_sidecar.get_connector(created["id"]) is None
    assert db.query(McpServer).filter(McpServer.id == created["server"]["id"]).first() is None


def test_launch_profiles_tool_is_never_exposed_to_the_agent():
    """F1.6: no built-in agent tool exposes `/api/launch-profiles` — a plain
    source scan of every agent-tool module, not an import (avoids pulling in
    unrelated optional dependencies just to prove a negative)."""
    import glob
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = []
    for path in glob.glob(os.path.join(root, "src", "agent_tools", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        if "launch-profiles" in content or "launch_profiles" in content:
            hits.append(path)
    assert hits == [], f"agent tool module(s) reference launch profiles: {hits}"


def test_list_presets_carries_the_defaults_the_form_prefills():
    """The Studio form prefills each placeholder from `preset.defaults`
    (APP_URL for both Hoards); the first live build serialised everything
    but that map, so APP_URL came up empty."""
    by_id = {p["id"]: p for p in connectors.list_presets()}
    assert by_id["jobhunter"]["defaults"]["APP_URL"] == "http://127.0.0.1:5178"
    assert by_id["writer"]["defaults"]["APP_URL"] == "http://127.0.0.1:8766"
    for preset in by_id.values():
        for key in preset["defaults"]:
            assert key in preset["placeholders"]
