"""A connector made from a preset follows that preset when its manifest changes
(src/connector_migration.py), and a slow local server gets a second attempt at
startup (src/mcp_manager.py::_connect_at_startup).

Nothing here touches a real install: the preset is a throwaway one and the
bridge script a file this test creates.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import src.connector_migration as connector_migration
import src.connector_sidecar as connector_sidecar
from core.database import McpServer
from src import connectors

pytestmark = pytest.mark.asyncio

OLD_COMMAND = "C:/Apps/Other Hoard.exe"
OLD_ARGS = ["bridge.cjs", "--module", "links"]


@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "linksapp"
    (d / "server").mkdir(parents=True)
    (d / "server" / "mcp.js").write_text("// inert bridge\n")
    (d / "data").mkdir()
    (d / "data" / "mcp-token").write_text("secret\n")
    return str(d)


@pytest.fixture
def preset(monkeypatch):
    p = connectors.ConnectorPreset(
        id="linkslike", name="Links like", purpose="test", capabilities=[], transport="stdio",
        command="node", args=["{APP_DIR}/server/mcp.js"],
        env={"APP_URL_ENV": "{APP_URL}", "APP_TOKEN_FILE": "{TOKEN_FILE}"},
        app_url_default="http://127.0.0.1:5181", health_path="/api/health", health_expect={},
        ui_url_default="{APP_URL}", placeholders=["APP_DIR", "APP_URL", "TOKEN_FILE"],
        launch_profile_hint={},
        defaults={"APP_URL": "http://127.0.0.1:5181", "TOKEN_FILE": "{APP_DIR}/data/mcp-token"},
    )
    monkeypatch.setitem(connectors.PRESETS, "linkslike", p)
    return p


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_sidecar, "DATA_DIR", str(tmp_path / "data"))

    async def not_this_app(preset, url):   # no test probes a real port
        return False

    monkeypatch.setattr(connector_migration, "_identifies", not_this_app)


@pytest.fixture
def db(tmp_path, monkeypatch):
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker

    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    McpServer.__table__.create(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(connector_migration, "SessionLocal", Session)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class _Manager:
    def __init__(self, status="disconnected"):
        self.status = status
        self.disconnected = []

    def get_server_status(self, server_id):
        return {"status": self.status}

    async def disconnect_server(self, server_id):
        self.disconnected.append(server_id)


def _make(db, *, command, args, env, values, preset_id="linkslike"):
    server = McpServer(id=str(uuid.uuid4()), name="Links", transport="stdio", command=command, args=json.dumps(args),
                       env=json.dumps(env), is_enabled=True)
    db.add(server)
    db.commit()
    entry = connector_sidecar.create_connector(
        preset_id=preset_id, server_id=server.id, owner=None, values=values,
        app_url=values.get("APP_URL", "http://127.0.0.1:8766"), ui_url=None)
    return server, entry


def _row(db, server):
    db.expire_all()
    s = db.query(McpServer).filter(McpServer.id == server.id).first()
    return s.command, json.loads(s.args), json.loads(s.env)


async def _no_finder(preset, *, current_url=""):
    return None


def _finder_for(app_dir, url="http://127.0.0.1:5181", dir_key="APP_DIR"):
    async def finder(preset, *, current_url=""):
        return SimpleNamespace(values={"APP_URL": url, dir_key: app_dir}, url=url)
    return finder


async def test_a_row_on_the_old_bridge_moves_to_the_presets_server(db, preset, app_dir, monkeypatch):
    # The shape seen live: old bridge, values from the old layout (a token file
    # of the other app, the other app's port), the new directory unknown.
    server, entry = _make(db, command=OLD_COMMAND, args=OLD_ARGS,
                          env={"OLD_URL": "http://127.0.0.1:8766", "OLD_TOKEN": "x"},
                          values={"APP_URL": "http://127.0.0.1:8766", "TOKEN_FILE": __file__})

    async def not_this_app(preset, url):
        return False

    monkeypatch.setattr(connector_migration, "_identifies", not_this_app)
    report = await connector_migration.migrate_connectors(_Manager(), finder=_finder_for(app_dir))

    assert [r["status"] for r in report] == ["migrated"]
    command, args, env = _row(db, server)
    assert command == "node"
    assert args == [f"{app_dir}/server/mcp.js"]
    assert env == {"APP_URL_ENV": "http://127.0.0.1:5181", "APP_TOKEN_FILE": f"{app_dir}/data/mcp-token"}
    saved = connector_sidecar.get_connector(entry["id"], redact=False)
    assert saved["values"]["APP_DIR"] == app_dir
    assert saved["values"]["APP_URL"] == "http://127.0.0.1:5181"
    assert saved["app_url"] == "http://127.0.0.1:5181"


async def test_the_migration_is_recorded_without_secrets(db, preset, app_dir, monkeypatch):
    _, entry = _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={}, values={"APP_DIR": app_dir})
    await connector_migration.migrate_connectors(_Manager(), finder=_no_finder)
    saved = connector_sidecar.get_connector(entry["id"], redact=False)
    (moved,) = saved["migrations"]
    assert moved["from"] == {"command": OLD_COMMAND, "args": OLD_ARGS}
    assert moved["to"]["command"] == "node"
    assert moved["env_keys"] == ["APP_TOKEN_FILE", "APP_URL_ENV"]
    assert "secret" not in json.dumps(moved) and app_dir not in json.dumps(moved["env_keys"])


async def test_values_the_user_set_survive(db, preset, app_dir, monkeypatch):
    custom_token = app_dir + "/data/custom-token"
    open(custom_token, "w").write("t")
    server, entry = _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={},
                          values={"APP_DIR": app_dir, "APP_URL": "http://127.0.0.1:5999"})

    async def identifies(preset, url):
        return True   # the app really answers on the user's port

    monkeypatch.setattr(connector_migration, "_identifies", identifies)
    await connector_migration.migrate_connectors(_Manager(), finder=_no_finder)
    _, _, env = _row(db, server)
    assert env["APP_URL_ENV"] == "http://127.0.0.1:5999"
    assert connector_sidecar.get_connector(entry["id"], redact=False)["values"]["APP_URL"] == "http://127.0.0.1:5999"


async def test_a_custom_token_file_is_kept_when_only_the_script_moved(db, preset, app_dir):
    custom_token = app_dir + "/data/custom-token"
    open(custom_token, "w").write("t")
    # Same program (node mcp.js), just launched from another folder: not a new layout.
    server, entry = _make(db, command="node", args=["D:/old-place/server/mcp.js"], env={},
                          values={"APP_DIR": app_dir, "TOKEN_FILE": custom_token})
    report = await connector_migration.migrate_connectors(_Manager(), finder=_no_finder)
    assert [r["status"] for r in report] == ["migrated"]
    _, args, env = _row(db, server)
    assert args == [f"{app_dir}/server/mcp.js"]
    assert env["APP_TOKEN_FILE"] == custom_token


async def test_the_same_paths_written_differently_are_not_a_migration(db, preset, app_dir):
    # Backslashes, another letter case and a blank value are not differences.
    win = lambda p: p.replace("/", "\\")  # noqa: E731
    server, entry = _make(db, command="NODE", args=[win(app_dir + "/server/mcp.js").upper()],
                          env={"APP_URL_ENV": "http://127.0.0.1:5181",
                               "APP_TOKEN_FILE": win(app_dir + "/data/mcp-token")},
                          values={"APP_DIR": app_dir, "TOKEN_FILE": ""})
    before = connector_sidecar.get_connector(entry["id"], redact=False)
    assert await connector_migration.migrate_connectors(_Manager(), finder=_no_finder) == []
    assert connector_sidecar.get_connector(entry["id"], redact=False) == before


async def test_a_command_template_with_placeholders_is_compared_by_program(db, tmp_path, monkeypatch):
    d = tmp_path / "py"
    (d / "bin").mkdir(parents=True)
    (d / "bin" / "python.exe").write_text("")
    (d / "bridge.py").write_text("")
    p = connectors.ConnectorPreset(
        id="pylike", name="Py", purpose="", capabilities=[], transport="stdio",
        command="{APP_DIR}/bin/python.exe", args=["-I", "{APP_DIR}/bridge.py"], env={},
        app_url_default="http://127.0.0.1:5999", health_path="/", health_expect={}, ui_url_default=None,
        placeholders=["APP_DIR"], launch_profile_hint={})
    monkeypatch.setitem(connectors.PRESETS, "pylike", p)
    server, _ = _make(db, command=str(d / "bin" / "python.exe"), args=["-I", str(d / "bridge.py")], env={},
                      values={"APP_DIR": str(d)}, preset_id="pylike")
    assert await connector_migration.migrate_connectors(_Manager(), finder=_no_finder) == []


async def test_a_row_already_in_step_is_left_alone(db, preset, app_dir):
    server, entry = _make(db, command="node", args=[f"{app_dir}/server/mcp.js"],
                          env={"APP_URL_ENV": "http://127.0.0.1:5181",
                               "APP_TOKEN_FILE": f"{app_dir}/data/mcp-token", "EXTRA": "kept"},
                          values={"APP_DIR": app_dir})
    before = connector_sidecar.get_connector(entry["id"], redact=False)
    assert await connector_migration.migrate_connectors(_Manager(), finder=_no_finder) == []
    assert _row(db, server)[2]["EXTRA"] == "kept"
    assert connector_sidecar.get_connector(entry["id"], redact=False) == before


async def test_blocked_when_the_directory_is_unknown_and_the_app_is_off(db, preset):
    server, entry = _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={"K": "v"},
                          values={"APP_URL": "http://127.0.0.1:8766"})
    report = await connector_migration.migrate_connectors(_Manager(), finder=_no_finder)
    assert report[0]["status"] == "blocked"
    assert any("APP_DIR" in r for r in report[0]["reasons"])
    assert _row(db, server) == (OLD_COMMAND, OLD_ARGS, {"K": "v"})   # untouched


async def test_a_background_pass_does_not_retry_a_blocked_connector_at_once(db, preset):
    calls = []

    async def counting_finder(preset, *, current_url=""):
        calls.append(1)
        return None

    _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={}, values={})
    connector_migration._blocked_at.clear()
    first = await connector_migration.migrate_connectors(_Manager(), finder=counting_finder, retry_blocked=False)
    again = await connector_migration.migrate_connectors(_Manager(), finder=counting_finder, retry_blocked=False)
    forced = await connector_migration.migrate_connectors(_Manager(), finder=counting_finder)
    assert [r["status"] for r in first] == ["blocked"]
    assert again == []
    assert [r["status"] for r in forced] == ["blocked"]
    assert len(calls) == 2


async def test_a_quick_pass_never_looks_for_the_app_and_leaves_such_rows_for_the_full_pass(db, preset, app_dir):
    server, entry = _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={"K": "v"}, values={})
    connector_migration._blocked_at.clear()

    async def exploding_finder(preset, *, current_url=""):
        raise AssertionError("a quick pass must not search the machine")

    assert await connector_migration.migrate_connectors(
        _Manager(), finder=exploding_finder, retry_blocked=False, quick=True) == []
    assert _row(db, server) == (OLD_COMMAND, OLD_ARGS, {"K": "v"})
    # Not stamped as blocked, so the full pass that follows does try.
    full = await connector_migration.migrate_connectors(
        _Manager(), finder=_finder_for(app_dir), retry_blocked=False)
    assert [r["status"] for r in full] == ["migrated"]


async def test_a_connected_server_is_disconnected_so_it_restarts_on_the_new_definition(db, preset, app_dir):
    server, _ = _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={}, values={"APP_DIR": app_dir})
    manager = _Manager(status="connected")
    await connector_migration.migrate_connectors(manager, finder=_no_finder)
    assert manager.disconnected == [server.id]


async def test_connectors_without_a_known_preset_are_ignored(db, preset, app_dir):
    server, _ = _make(db, command=OLD_COMMAND, args=OLD_ARGS, env={}, values={}, preset_id="gone")
    assert await connector_migration.migrate_connectors(_Manager(), finder=_no_finder) == []
    assert _row(db, server)[0] == OLD_COMMAND


async def test_the_shipped_links_preset_replaces_the_compatibility_bridge(db, tmp_path):
    """The real manifest: Links is its own app (node server/mcp.js)."""
    links = connectors.get_preset("links")
    assert links is not None and links.command == "node" and links.args[0].endswith("/server/mcp.js")
    dir_key = next(p for p in links.placeholders if p.endswith("_DIR"))
    d = tmp_path / "Links"
    (d / "server").mkdir(parents=True)
    (d / "server" / "mcp.js").write_text("//\n")
    server, entry = _make(
        db, command="C:/Program Files/Writers Hoard/Writers Hoard.exe",
        args=["resources/mcpStdio.cjs", "--publishing-module", "links"],
        env={"WH_BRIDGE_URL": "http://127.0.0.1:8766"},
        values={"APP_URL": "http://127.0.0.1:8766", "TOKEN_FILE": "C:/Users/x/AppData/Roaming/writers-hoard/token"},
        preset_id="links")

    report = await connector_migration.migrate_connectors(
        _Manager(), finder=_finder_for(str(d), "http://127.0.0.1:5181", dir_key))

    assert [r["status"] for r in report] == ["migrated"]
    command, args, env = _row(db, server)
    assert (command, args) == ("node", [f"{d}/server/mcp.js"])
    assert env == {"LINKS_URL": "http://127.0.0.1:5181", "LINKS_TOKEN_FILE": f"{d}/data/mcp-token"}


async def test_startup_gives_a_slow_local_server_a_second_longer_attempt(monkeypatch):
    from src.mcp_manager import McpManager

    manager = McpManager()
    calls = []

    async def fake(srv, *, _oauth_provider=None, timeout=20):
        calls.append(timeout)
        manager._connections[srv.id] = {"status": "timeout" if len(calls) == 1 else "connected"}

    monkeypatch.setattr(manager, "_connect_with_timeout", fake)
    await manager._connect_at_startup(SimpleNamespace(id="s1", name="Links", transport="stdio"))
    assert calls == [20, 60.0]


async def test_startup_does_not_retry_what_connected_or_what_is_remote(monkeypatch):
    from src.mcp_manager import McpManager

    manager = McpManager()
    calls = []

    async def fake(srv, *, _oauth_provider=None, timeout=20):
        calls.append(srv.id)
        manager._connections[srv.id] = {"status": "connected" if srv.id == "ok" else "timeout"}

    monkeypatch.setattr(manager, "_connect_with_timeout", fake)
    await manager._connect_at_startup(SimpleNamespace(id="ok", name="a", transport="stdio"))
    await manager._connect_at_startup(SimpleNamespace(id="remote", name="b", transport="http"))
    assert calls == ["ok", "remote"]
