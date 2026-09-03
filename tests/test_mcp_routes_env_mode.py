"""The env mode as the MCP API reports and sets it (routes/mcp/mcp_routes.py).

Every read of a server's config says which environment its child process gets,
so the UI can show it rather than the user guessing why a server can or cannot
see a variable. And the whole point of the migration: adding a server today
starts it minimal without changing a single server that already exists.

The route handlers are called directly, the way tests/test_browser_mcp_settings.py
already exercises this router: a TestClient portal per test does not survive
these async handlers reliably under the repo's pytest-asyncio auto mode, and the
thing under test here is the handler's logic, not Starlette's form parsing.
"""

import asyncio
import json

import pytest

import routes.mcp.mcp_routes as mcp_routes
import src.settings as settings_mod
from core.database import McpServer
from fastapi import HTTPException
from src import mcp_manager as mm
from src.mcp_manager import McpManager


@pytest.fixture
def logs(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(mm, "DATA_DIR", str(d))
    return d


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real, file-backed mcp_servers table the routes write through.

    Not the session-wide `sqlite:///:memory:`: each connection to that gets its
    own empty database, so a row inserted here would not exist for the request.
    """
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker

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
def manager(monkeypatch):
    """A McpManager whose connect/disconnect only record what they were asked."""
    mgr = McpManager()
    connected = {}

    async def fake_connect(server_id, name, transport, command=None, args=None,
                           env=None, url=None, inherit_env=None):
        connected[server_id] = {"inherit_env": inherit_env, "env": env}
        mgr._connections[server_id] = {
            "status": "connected", "name": name, "transport": transport,
            "tool_count": 0, "inherit_env": True if inherit_env is None else bool(inherit_env),
        }
        return True

    async def fake_disconnect(server_id):
        mgr._connections.pop(server_id, None)

    monkeypatch.setattr(mgr, "connect_server", fake_connect)
    monkeypatch.setattr(mgr, "disconnect_server", fake_disconnect)
    mgr.connected = connected
    return mgr


@pytest.fixture
def routes(monkeypatch, manager, logs):
    monkeypatch.setattr(mcp_routes, "require_admin", lambda request: None)
    router = mcp_routes.setup_mcp_routes(manager)
    by_path = {}
    for route in router.routes:
        for method in getattr(route, "methods", ()) or ():
            by_path[(method, route.path)] = route.endpoint
    return by_path


@pytest.fixture
def min_env(monkeypatch):
    values = {}
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda k, d=None: values.get(k, real(k, d)))
    return values


REQ = object()


def _call(fn, **kwargs):
    result = fn(request=REQ, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


# ── reads say which mode a server is in ───────────────────────────────────

def test_the_server_list_reports_the_env_mode(routes, db, logs):
    db.add(McpServer(id="old", name="Gmail", transport="stdio", command="npx",
                     args="[]", env=json.dumps({"GMAIL": "x"}), is_enabled=True,
                     inherit_env=True))
    db.add(McpServer(id="new", name="Notes", transport="stdio", command="node",
                     args="[]", env="{}", is_enabled=True, inherit_env=False))
    db.commit()

    rows = {r["id"]: r for r in _call(routes[("GET", "/api/mcp/servers")])}
    assert rows["old"]["inherit_env"] is True and rows["old"]["env_mode"] == "inherited"
    assert rows["new"]["inherit_env"] is False and rows["new"]["env_mode"] == "minimal"
    # And where to look when it will not start.
    assert rows["old"]["stderr_log"].endswith("old.stderr.log")
    assert rows["old"]["stderr_log"].startswith(str(logs))


def test_a_row_written_before_the_column_reads_as_inherited(routes, db, logs):
    """The NULL a partially migrated DB leaves behind must read as inherit."""
    db.add(McpServer(id="legacy", name="Legacy", transport="stdio", command="node",
                     args="[]", env="{}", is_enabled=True))
    db.commit()
    db.query(McpServer).filter(McpServer.id == "legacy").update({"inherit_env": None})
    db.commit()

    row = _call(routes[("GET", "/api/mcp/servers")])[0]
    assert row["inherit_env"] is True and row["env_mode"] == "inherited"


# ── the migration guarantee, through the API ──────────────────────────────

def test_an_existing_server_keeps_the_full_environment_on_reconnect(routes, db, manager, min_env, logs):
    """The rule that outranks everything: nothing already working may change."""
    min_env["agent_mcp_min_env"] = True          # the new default is minimal…
    db.add(McpServer(id="old", name="Gmail", transport="stdio", command="npx",
                     args="[]", env=json.dumps({"GMAIL": "x"}), is_enabled=True,
                     inherit_env=True))
    db.commit()

    body = _call(routes[("POST", "/api/mcp/servers/{server_id}/reconnect")], server_id="old")
    assert body["inherit_env"] is True
    assert manager.connected["old"]["inherit_env"] is True   # …and this one is untouched

    # Same through the enable toggle, which is the other reconnect path.
    manager.connected.clear()
    _call(routes[("PATCH", "/api/mcp/servers/{server_id}")], server_id="old", is_enabled="true")
    assert manager.connected["old"]["inherit_env"] is True


def test_a_newly_added_server_starts_minimal(routes, db, manager, min_env, logs):
    min_env["agent_mcp_min_env"] = True
    body = _call(routes[("POST", "/api/mcp/servers")],
                 name="Third party", transport="stdio", command="node",
                 args=json.dumps(["server.js"]), env=json.dumps({"MY_TOKEN": "abc"}),
                 url=None, oauth_file=None, oauth_config=None, inherit_env=None)

    assert body["inherit_env"] is False and body["env_mode"] == "minimal"
    assert manager.connected[body["id"]]["inherit_env"] is False
    stored = db.query(McpServer).filter(McpServer.id == body["id"]).first()
    assert stored.inherit_env is False


def test_the_setting_off_keeps_new_servers_on_the_old_behaviour(routes, db, manager, min_env, logs):
    min_env["agent_mcp_min_env"] = False
    body = _call(routes[("POST", "/api/mcp/servers")],
                 name="Third party", transport="stdio", command="node",
                 args="[]", env="{}", url=None, oauth_file=None,
                 oauth_config=None, inherit_env=None)
    assert body["inherit_env"] is True
    assert manager.connected[body["id"]]["inherit_env"] is True


@pytest.mark.parametrize("form_value,expected", [("true", True), ("false", False),
                                                 ("1", True), ("0", False)])
def test_the_form_can_override_the_default_either_way(routes, db, manager, min_env, logs,
                                                      form_value, expected):
    min_env["agent_mcp_min_env"] = True
    body = _call(routes[("POST", "/api/mcp/servers")],
                 name="Explicit", transport="stdio", command="node",
                 args="[]", env="{}", url=None, oauth_file=None,
                 oauth_config=None, inherit_env=form_value)
    assert body["inherit_env"] is expected


# ── switching one server ──────────────────────────────────────────────────

def test_env_mode_can_be_changed_per_server_and_reconnects(routes, db, manager, logs):
    db.add(McpServer(id="old", name="Gmail", transport="stdio", command="npx",
                     args="[]", env="{}", is_enabled=True, inherit_env=True))
    db.commit()
    endpoint = routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")]

    body = _call(endpoint, server_id="old", inherit_env="false")
    assert body["inherit_env"] is False and body["env_mode"] == "minimal"
    assert manager.connected["old"]["inherit_env"] is False
    db.expire_all()
    assert db.query(McpServer).filter(McpServer.id == "old").first().inherit_env is False

    # …and back again, because a user who breaks their server must be able to
    # undo it without deleting and re-adding it.
    body = _call(endpoint, server_id="old", inherit_env="true")
    assert body["inherit_env"] is True
    assert manager.connected["old"]["inherit_env"] is True


def test_env_mode_on_a_disabled_server_does_not_connect_it(routes, db, manager, logs):
    db.add(McpServer(id="off", name="Off", transport="stdio", command="node",
                     args="[]", env="{}", is_enabled=False, inherit_env=True))
    db.commit()
    body = _call(routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")],
                 server_id="off", inherit_env="false")
    assert body["inherit_env"] is False and body["connected"] is None
    assert "off" not in manager.connected


def test_env_mode_on_an_unknown_server_is_a_404(routes, db, logs):
    with pytest.raises(HTTPException) as exc:
        _call(routes[("PATCH", "/api/mcp/servers/{server_id}/env-mode")],
              server_id="nope", inherit_env="false")
    assert exc.value.status_code == 404


# ── the stderr surface ────────────────────────────────────────────────────

def test_the_stderr_route_returns_the_tail(routes, db, logs):
    import os
    db.add(McpServer(id="srv", name="S", transport="stdio", command="node",
                     args="[]", env="{}", is_enabled=True, inherit_env=True))
    db.commit()
    path = mm.stderr_log_path("srv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("boot\n/bin/sh: 1: node: not found\n")

    body = _call(routes[("GET", "/api/mcp/servers/{server_id}/stderr")], server_id="srv")
    assert body["path"] == path
    assert "node: not found" in body["tail"]


def test_the_stderr_route_404s_for_an_unknown_server(routes, db, logs):
    with pytest.raises(HTTPException) as exc:
        _call(routes[("GET", "/api/mcp/servers/{server_id}/stderr")], server_id="nope")
    assert exc.value.status_code == 404


def test_the_stderr_route_is_empty_when_nothing_was_logged(routes, db, logs):
    db.add(McpServer(id="quiet", name="Q", transport="stdio", command="node",
                     args="[]", env="{}", is_enabled=True, inherit_env=True))
    db.commit()
    body = _call(routes[("GET", "/api/mcp/servers/{server_id}/stderr")], server_id="quiet")
    assert body["tail"] == ""
