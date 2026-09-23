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
import os
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


def test_a_module_bridge_is_not_checked_as_a_file(tmp_path):
    """`python -m pkg.mcp` has "-m" as its first argument; adopting such an
    app failed with "bridge script not found: -m"."""
    import dataclasses
    base = connectors.get_preset("jobhunter")
    preset = dataclasses.replace(base, args=["-m", "pkg.hub.mcp"])
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    resolved = connectors.resolve_preset_values(
        preset, {"JOBHUNT_DIR": str(app_dir), "APP_URL": "http://127.0.0.1:5178"})
    assert resolved["ok"] is True, resolved
    assert resolved["args"] == ["-m", "pkg.hub.mcp"]


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


def test_jobhunter_token_file_defaults_next_to_the_app_data_and_is_overridable(tmp_path):
    """`TOKEN_FILE` defaults to `{JOBHUNT_DIR}/data/mcp-token` (a default that
    names another placeholder, resolved against the user's JOBHUNT_DIR), and
    a test instance started with its own JOBHUNT_DATA_DIR can point the
    bridge at the token that instance wrote."""
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "mcp.js").write_text("// bridge")
    preset = connectors.get_preset("jobhunter")
    resolved = connectors.resolve_preset_values(preset, {"JOBHUNT_DIR": str(tmp_path)})
    assert resolved["ok"], resolved
    assert resolved["env"]["JOBHUNT_TOKEN_FILE"] == str(tmp_path) + "/data/mcp-token"
    assert "{" not in resolved["env"]["JOBHUNT_TOKEN_FILE"]
    other = tmp_path / "elsewhere" / "mcp-token"
    resolved = connectors.resolve_preset_values(preset, {"JOBHUNT_DIR": str(tmp_path), "TOKEN_FILE": str(other)})
    assert resolved["env"]["JOBHUNT_TOKEN_FILE"] == str(other)


def test_redaction_spares_paths_to_secrets_and_patch_ignores_the_marker(tmp_path, monkeypatch):
    """`TOKEN_FILE` is where the bridge reads its token, not the token: the
    form must show the path (the first live build showed "***redacted***"
    and would have saved it back). A value equal to the marker sent on
    PATCH means "unchanged"."""
    monkeypatch.setattr(connector_sidecar, "DATA_DIR", str(tmp_path))
    assert connector_sidecar.is_secret_key("TOKEN_FILE") is False
    assert connector_sidecar.is_secret_key("WH_BRIDGE_TOKEN_FILE") is False
    assert connector_sidecar.is_secret_key("API_TOKEN") is True
    assert connector_sidecar.is_secret_key("SECRET") is True
    entry = connector_sidecar.create_connector(
        preset_id="jobhunter", server_id="srv1", owner="admin",
        values={"JOBHUNT_DIR": str(tmp_path), "APP_URL": "http://127.0.0.1:5178", "TOKEN_FILE": "/x/mcp-token", "API_TOKEN": "s3cret"},
        app_url="http://127.0.0.1:5178", ui_url="http://127.0.0.1:5178", launch_profile_id=None,
    )
    shown = connector_sidecar.get_connector(entry["id"], redact=True)["values"]
    assert shown["TOKEN_FILE"] == "/x/mcp-token"
    assert shown["API_TOKEN"] == connector_sidecar.REDACTED


# ── 17-09: follow the app / nearby apps ──────────────────────────────────────

def _fingerprint_server(health_server, service="jubhunters-hoard"):
    return health_server(200, json.dumps({"service": service, "version": "0.2.0"}).encode())


async def test_check_follows_the_app_to_its_new_port(routes, db, manager, bridge_dir, health_server, monkeypatch):
    """Jobhunter takes the first free port from 5178 up: the stored APP_URL
    dies, the app answers on another port. A check moves the connector."""
    from src import connector_discovery
    live = _fingerprint_server(health_server)
    live_port = int(live.rsplit(":", 1)[1])
    body = {"preset_id": "jobhunter", "values": {"JOBHUNT_DIR": bridge_dir,
                                                  "APP_URL": "http://127.0.0.1:1"}}  # nothing listens on :1
    created = await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    server_id = created["server"]["id"]
    await manager.connect_server(server_id=server_id, name="x", transport="stdio", command="node", args=[])
    # The scan sees only our fake app.
    monkeypatch.setattr(connector_discovery, "listening_ports",
                        lambda: [connector_discovery.ListeningPort(port=live_port, process="node", cwd=bridge_dir)])

    status = await routes[("POST", "/api/app-connectors/{connector_id}/check")](
        connector_id=created["id"], request=FakeRequest())
    assert status["relocated_from"] == "http://127.0.0.1:1"
    assert status["app_url"] == live
    row = db.query(McpServer).filter(McpServer.id == server_id).first()
    assert json.loads(row.env)["JOBHUNT_URL"] == live
    assert server_id in manager.calls["disconnect"]  # respawn with the new env
    entry = connector_sidecar.get_connector(created["id"], redact=False)
    assert entry["app_url"] == live and entry["values"]["APP_URL"] == live


async def test_check_leaves_a_reachable_app_alone(routes, db, bridge_dir, health_server, monkeypatch):
    from src import connector_discovery
    live = _fingerprint_server(health_server)
    body = {"preset_id": "jobhunter", "values": {"JOBHUNT_DIR": bridge_dir, "APP_URL": live}}
    created = await routes[("POST", "/api/app-connectors")](request=FakeRequest(body=body))
    monkeypatch.setattr(connector_discovery, "listening_ports", lambda: (_ for _ in ()).throw(AssertionError("no scan")))
    status = await routes[("POST", "/api/app-connectors/{connector_id}/check")](
        connector_id=created["id"], request=FakeRequest())
    assert "relocated_from" not in status and status["app"]["reachable"] is True


async def test_discover_lists_nearby_apps_with_preset_and_install_dir(routes, db, bridge_dir, health_server, monkeypatch):
    from src import connector_discovery
    live = _fingerprint_server(health_server)
    other = health_server(200, b"not json")
    live_port, other_port = int(live.rsplit(":", 1)[1]), int(other.rsplit(":", 1)[1])
    monkeypatch.setattr(connector_discovery, "listening_ports", lambda: [
        connector_discovery.ListeningPort(port=live_port, pid=42, process="node", cwd=bridge_dir),
        connector_discovery.ListeningPort(port=other_port, pid=43, process="python"),
        connector_discovery.ListeningPort(port=11434, process="ollama"),   # skipped, never probed
    ])
    out = await routes[("GET", "/api/app-connectors/discover")](request=FakeRequest())
    apps = out["apps"]
    assert [a["port"] for a in apps][:1] == [live_port]  # recognised first
    hit = apps[0]
    assert hit["preset_id"] == "jobhunter" and hit["process"] == "node"
    assert hit["values"] == {"APP_URL": live, "JOBHUNT_DIR": bridge_dir}
    assert hit["missing"] == [] and hit["connector_id"] is None
    assert all(a["port"] != 11434 for a in apps)


async def test_adopt_creates_the_connector_from_a_discovered_app(routes, db, bridge_dir, health_server, monkeypatch):
    from src import connector_discovery
    live = _fingerprint_server(health_server)
    live_port = int(live.rsplit(":", 1)[1])
    monkeypatch.setattr(connector_discovery, "listening_ports", lambda: [
        connector_discovery.ListeningPort(port=live_port, pid=42, process="node", cwd=bridge_dir)])
    created = await routes[("POST", "/api/app-connectors/adopt")](request=FakeRequest(body={"port": live_port}))
    assert created["preset_id"] == "jobhunter"
    assert created["app_url"] == live and created["values"]["JOBHUNT_DIR"] == bridge_dir
    # Now discovery reports it as already connected.
    out = await routes[("GET", "/api/app-connectors/discover")](request=FakeRequest())
    assert out["apps"][0]["connector_id"] == created["id"]


def test_match_preset_by_service_then_title():
    from src.connector_discovery import match_preset
    assert match_preset({"service": "jubhunters-hoard"}, "") == "jobhunter"
    assert match_preset({"service": "writers-hoard-ai-bridge"}, "") == "writer"
    assert match_preset(None, "Jubhunter's Hoard") == "jobhunter"
    assert match_preset({"service": "something-else"}, "My app") is None


# ── the three "apps" presets (dorian, gepetto, platos) ─────────────────────

def test_dorian_is_a_native_stdio_preset(tmp_path):
    d = tmp_path / "dorian"
    (d / "selfhoard").mkdir(parents=True)
    (d / "selfhoard" / "mcp_server.py").write_text("# fake\n")
    preset = connectors.get_preset("dorian")
    assert preset.name == "Dorian's Hoard"
    resolved = connectors.resolve_preset_values(preset, {"DORIAN_DIR": str(d)})
    assert resolved["ok"], resolved
    norm = lambda x: str(x).replace("\\", "/")   # presets join with "/" whatever the OS
    assert norm(resolved["command"]) == norm(d / ".venv" / "Scripts" / "python.exe")
    assert [norm(a) for a in resolved["args"]] == [
        norm(d / "selfhoard" / "mcp_server.py"),
        "--credential-file",
        norm(d / "data" / "agent-clients" / "faustus.json"),
    ]
    assert resolved["app_url"] == "http://127.0.0.1:8741"
    assert resolved["ui_url"] == "http://127.0.0.1:8741"
    assert preset.health_path == "/api/session"
    assert preset.health_expect == {"mode": "local"}


def test_gepetto_and_platos_run_faustus_own_rest_bridge(tmp_path):
    d = tmp_path / "gepetto"
    d.mkdir()
    preset = connectors.get_preset("gepetto")
    assert preset.name == "Gepetto's Hoard"
    resolved = connectors.resolve_preset_values(preset, {"GEPETTO_DIR": str(d)})
    assert resolved["ok"], resolved
    # {FAUSTUS_PYTHON} and {FAUSTUS_DIR} are filled in automatically — never
    # user-supplied, never reported as missing.
    import sys
    from src.constants import BASE_DIR
    assert resolved["command"] == sys.executable
    assert [a.replace("\\", "/") for a in resolved["args"]] == [BASE_DIR.rstrip("/\\").replace("\\", "/") + "/bridges/rest_mcp/server.py"]
    assert resolved["env"]["REST_BASE_URL"] == "http://127.0.0.1:8767"
    assert resolved["env"]["REST_OPENAPI_URL"] == "http://127.0.0.1:8767/openapi.json"
    assert resolved["env"]["REST_MANIFEST"].endswith("manifests/gepetto.json")
    assert resolved["ui_url"] == "http://127.0.0.1:8767"
    assert preset.health_expect == {"application": "sculptors-hoard"}

    d2 = tmp_path / "platos"
    d2.mkdir()
    preset2 = connectors.get_preset("platos")
    assert preset2.name == "Plato's Hoard"
    resolved2 = connectors.resolve_preset_values(preset2, {"PLATOS_DIR": str(d2)})
    assert resolved2["ok"], resolved2
    assert resolved2["env"]["REST_BASE_URL"] == "http://127.0.0.1:5000"
    assert "REST_OPENAPI_URL" not in resolved2["env"]
    assert resolved2["env"]["REST_MANIFEST"].endswith("manifests/platos.json")
    assert preset2.health_path == "/"
    assert preset2.health_expect == {}


def test_faustus_placeholders_are_never_missing():
    """FAUSTUS_DIR/FAUSTUS_PYTHON are not declared placeholders (a user never
    fills them in) and resolve without ever being reported as missing."""
    preset = connectors.get_preset("gepetto")
    assert "FAUSTUS_DIR" not in preset.placeholders
    assert "FAUSTUS_PYTHON" not in preset.placeholders
    resolved = connectors.resolve_preset_values(preset, {})
    assert resolved["ok"] is False
    assert resolved["missing"] == ["GEPETTO_DIR"]


def test_new_presets_carry_their_own_launch_profile_hint():
    for preset_id, dir_key in (("dorian", "DORIAN_DIR"), ("gepetto", "GEPETTO_DIR"), ("platos", "PLATOS_DIR")):
        preset = connectors.get_preset(preset_id)
        hint = preset.launch_profile_hint
        assert hint["cwd"] == "{" + dir_key + "}"
        assert hint.get("executable") or hint.get("argv") is not None


def test_list_presets_includes_the_three_apps_presets():
    by_id = {p["id"]: p for p in connectors.list_presets()}
    for preset_id in ("dorian", "gepetto", "platos"):
        assert preset_id in by_id
        assert by_id[preset_id]["transport"] == "stdio"


async def test_health_sends_the_bridge_token_only_after_a_401(bridge_dir, tmp_path):
    """Writer's Hoard answers 401 to an anonymous /api/health: with the
    connector's TOKEN_FILE the probe retries once with the bearer token and
    the fingerprint check then passes."""
    import http.server, threading
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("Authorization"))
            if self.headers.get("Authorization") != "Bearer sekret":
                self.send_response(401); self.end_headers(); return
            body = json.dumps({"service": "writers-hoard-ai-bridge"}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(body)

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        tok = tmp_path / "token"; tok.write_text("sekret\n")
        wdir = tmp_path / "writer"; (wdir / "dist-electron" / "aibridge").mkdir(parents=True)
        (wdir / "dist-electron" / "aibridge" / "mcpStdio.cjs").write_text("// fake\n")
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        status = await connector_status.compute_status(
            connectors.get_preset("writer"),
            values={"WRITER_DIR": str(wdir), "APP_URL": url, "TOKEN_FILE": str(tok)},
            is_enabled=True, connector_id="w1", manager_status={"status": "connected", "tool_count": 5},
            force_check=True)
        assert seen == [None, "Bearer sekret"]
        assert status["state"] == "available", status
    finally:
        httpd.shutdown(); httpd.server_close()


async def test_adopt_installs_an_app_that_declares_itself(routes, db, health_server, tmp_path, monkeypatch):
    """Seen live: discovery recognised an app by its own faustus-plugin.json
    (``declares_itself``), and Add answered "Unknown preset". Adopting it now
    installs the declared manifest under DATA_DIR/plugins and connects."""
    from src import connector_discovery, plugins as plugins_mod

    app = tmp_path / "ledger-app"
    app.mkdir()
    (app / "mcp.js").write_text("// bridge\n")
    (app / plugins_mod.APP_MANIFEST_NAME).write_text(json.dumps({
        "schema": 1, "id": "ledger-test", "name": "Ledger (test)",
        "placeholders": ["LEDGER_DIR", "APP_URL"],
        "app": {"url_default": "http://127.0.0.1:8790",
                "health": {"path": "/api/health", "expect": {}},
                "identify": {"service": ["ledger-test"]}},
        "mcp": {"command": "node", "args": ["{LEDGER_DIR}/mcp.js"],
                "env": {"LEDGER_URL": "{APP_URL}"}},
    }), encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "user_dir", lambda: str(tmp_path / "installed"))
    plugins_mod.reset_cache()
    live = health_server(200, json.dumps({"service": "ledger-test"}).encode())
    port = int(live.rsplit(":", 1)[1])
    monkeypatch.setattr(connector_discovery, "listening_ports", lambda: [
        connector_discovery.ListeningPort(port=port, pid=7, process="node", cwd=str(app))])
    try:
        assert connectors.get_preset("ledger-test") is None
        created = await routes[("POST", "/api/app-connectors/adopt")](request=FakeRequest(body={"port": port}))
        assert created["preset_id"] == "ledger-test"
        assert created["app_url"] == live and created["values"]["LEDGER_DIR"] == str(app)
        assert (tmp_path / "installed" / "ledger-test" / "plugin.json").is_file()
    finally:
        connectors.PRESETS.pop("ledger-test", None)
        plugins_mod.reset_cache()


@pytest.fixture(autouse=True)
def _fresh_shared_probes():
    from src import connector_discovery as cd
    cd._shared_probes.clear()
    cd._shared_inflight.clear()
    yield
    cd._shared_probes.clear()
    cd._shared_inflight.clear()


@pytest.mark.asyncio
async def test_concurrent_find_app_calls_share_one_scan(monkeypatch):
    """Five switched-off apps followed at once used to scan every loopback
    port five times (a 504 on a forced refresh, seen live)."""
    import asyncio as _asyncio
    from src import connector_discovery as cd

    ports = [cd.ListeningPort(port=p) for p in (41001, 41002, 41003)]
    calls = []

    async def fake_discover(*, ports=None, exclude=None):
        calls.append(sorted(lp.port for lp in ports))
        await _asyncio.sleep(0.05)
        return [cd.Candidate(port=41002, url="http://127.0.0.1:41002", pid=None, process="",
                             cwd="", title="", health={"service": "x"}, preset_id="jobhunter",
                             preset_name="Jobhunter", latency_ms=1, values={})]

    monkeypatch.setattr(cd, "discover", fake_discover)
    job = connectors.get_preset("jobhunter")
    writer = connectors.get_preset("writer")
    got = await _asyncio.gather(*(cd.find_app(p, ports=ports) for p in (job, writer, job, writer)))
    assert got[0] is not None and got[0].port == 41002 and got[2] is not None
    assert got[1] is None and got[3] is None
    probed = [p for c in calls for p in c]
    assert sorted(probed) == sorted(set(probed)), f"a port was probed twice: {calls}"
    calls.clear()
    assert (await cd.find_app(job, ports=ports)).port == 41002
    assert calls == []  # answered from the shared probes
