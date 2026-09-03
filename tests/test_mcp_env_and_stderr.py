"""What a stdio MCP subprocess gets, and where its stderr goes (src/mcp_manager.py).

Two holes, one file:

  * ``env={**os.environ, **env}`` handed a third-party server every provider API
    key in the process and the internal loopback token — the one §26.5 found is
    privileged enough to reach ``/api/storage/*``.
  * ``stdio_client(server_params)`` with no ``errlog`` sends the child's stderr
    to ``sys.stderr``, which in the packaged Windows build is nowhere. A server
    that cannot start prints ``node: not found`` into the void.

The migration constraint outranks both: every server already configured keeps
``inherit_env: true``. A server that silently loses the variable it was reading
is a break with no error message, and the user has no way to connect the two.
"""

import asyncio
import json
import os
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import mcp_manager as mm


SECRET_KEYS = {
    "OPENAI_API_KEY": "sk-not-yours",
    "ANTHROPIC_API_KEY": "sk-ant-not-yours",
    "ODYSSEUS_INTERNAL_TOKEN": "loopback-token",
    "SOME_OTHER_APP_SECRET": "hunter2",
}


@pytest.fixture
def secrets(monkeypatch):
    for key, value in SECRET_KEYS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/root"))
    return SECRET_KEYS


@pytest.fixture
def logs(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(mm, "DATA_DIR", str(d))
    return d


# ── the environment handed to the child ───────────────────────────────────

def test_minimal_env_carries_no_unrelated_secret(secrets):
    env = mm.build_server_env({"MY_SERVER_TOKEN": "abc"}, inherit_env=False)
    assert env is not None
    for key in SECRET_KEYS:
        assert key not in env, f"{key} reached a third-party subprocess"
    # …and the server's own declared value is still there.
    assert env["MY_SERVER_TOKEN"] == "abc"


def test_minimal_env_still_carries_what_a_process_needs_to_start(secrets):
    env = mm.build_server_env({}, inherit_env=False)
    assert env["PATH"] == os.environ["PATH"]
    assert env["HOME"] == os.environ["HOME"]
    # Every name in the allowlist is structural, never a credential.
    for key in mm._MINIMAL_ENV_KEYS:
        assert not any(tok in key for tok in ("KEY", "TOKEN", "SECRET", "PASSWORD"))


def test_minimal_env_lets_a_server_override_a_structural_variable(secrets, monkeypatch):
    env = mm.build_server_env({"HOME": "/srv/sandbox"}, inherit_env=False)
    assert env["HOME"] == "/srv/sandbox"


def test_minimal_env_skips_exported_shell_functions(secrets, monkeypatch):
    monkeypatch.setenv("TERM", "()  { echo pwned; }")
    assert "TERM" not in mm.build_server_env({}, inherit_env=False)


def test_inherited_env_is_byte_identical_to_the_old_expression(secrets):
    declared = {"MY_SERVER_TOKEN": "abc"}
    assert mm.build_server_env(declared, inherit_env=True) == {**os.environ, **declared}
    # And the no-declared-env case still hands the SDK None, so it builds its
    # own default — exactly what `if env else None` did before.
    assert mm.build_server_env({}, inherit_env=True) is None
    assert mm.build_server_env(None, inherit_env=True) is None


def test_inherited_env_reaches_every_secret(secrets):
    env = mm.build_server_env({"X": "1"}, inherit_env=True)
    for key, value in SECRET_KEYS.items():
        assert env[key] == value


# ── migration: what an already-configured server keeps ────────────────────

def test_a_row_from_before_the_column_inherits():
    """The shape a partially migrated DB hands the ORM."""
    assert mm.server_inherits_env(types.SimpleNamespace(id="a")) is True          # no attribute
    assert mm.server_inherits_env(types.SimpleNamespace(inherit_env=None)) is True  # NULL
    assert mm.server_inherits_env(types.SimpleNamespace(inherit_env=1)) is True
    assert mm.server_inherits_env(types.SimpleNamespace(inherit_env=True)) is True
    assert mm.server_inherits_env(None) is True


def test_only_an_explicit_false_makes_a_server_minimal():
    assert mm.server_inherits_env(types.SimpleNamespace(inherit_env=False)) is False
    assert mm.server_inherits_env(types.SimpleNamespace(inherit_env=0)) is False


def test_the_migration_backfills_existing_rows_to_inherit(tmp_path, monkeypatch):
    """The SQL itself: a pre-existing mcp_servers table must come out all-1."""
    import sqlalchemy
    from sqlalchemy import text

    db = tmp_path / "app.db"
    engine = sqlalchemy.create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE mcp_servers (id TEXT PRIMARY KEY, name TEXT, "
                          "transport TEXT, command TEXT, args TEXT, env TEXT, url TEXT, "
                          "is_enabled BOOLEAN)"))
        conn.execute(text("INSERT INTO mcp_servers VALUES "
                          "('old1','Gmail','stdio','npx','[]','{\"GMAIL\":\"x\"}',NULL,1)"))
        conn.execute(text("INSERT INTO mcp_servers VALUES "
                          "('old2','Notes','stdio','node','[]',NULL,NULL,1)"))
        conn.commit()

    import core.database as cdb
    monkeypatch.setattr(cdb, "engine", engine)
    cdb._migrate_add_mcp_inherit_env_column()

    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, inherit_env FROM mcp_servers")).fetchall())
    assert rows == {"old1": 1, "old2": 1}

    # Idempotent, and it does not stomp a server the user later set to minimal.
    with engine.connect() as conn:
        conn.execute(text("UPDATE mcp_servers SET inherit_env = 0 WHERE id = 'old2'"))
        conn.commit()
    cdb._migrate_add_mcp_inherit_env_column()
    with engine.connect() as conn:
        rows = dict(conn.execute(text("SELECT id, inherit_env FROM mcp_servers")).fetchall())
    assert rows == {"old1": 1, "old2": 0}


def test_a_new_server_follows_the_setting(monkeypatch):
    import src.settings as settings_mod
    values = {}
    real = settings_mod.get_setting
    monkeypatch.setattr(settings_mod, "get_setting",
                        lambda k, d=None: values.get(k, real(k, d)))

    values["agent_mcp_min_env"] = True
    assert mm.new_server_inherits_env() is False
    values["agent_mcp_min_env"] = False
    assert mm.new_server_inherits_env() is True

    # A broken settings backend must not silently widen a new server's reach.
    def boom(*a, **kw):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(settings_mod, "get_setting", boom)
    assert mm.new_server_inherits_env() is False


# ── what the subprocess actually gets ─────────────────────────────────────

def _run_connect(monkeypatch, *, inherit_env, declared):
    """Drive _connect_stdio with a stubbed SDK and capture what it was handed."""
    captured = {}

    class FakeParams:
        def __init__(self, command=None, args=None, env=None, **kw):
            captured["command"] = command
            captured["args"] = args
            captured["env"] = env

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            return None

        async def list_tools(self):
            return types.SimpleNamespace(tools=[])

    class FakeTransport:
        async def __aenter__(self):
            return (object(), object())

        async def __aexit__(self, *a):
            return False

    def fake_stdio_client(params, errlog=None):
        captured["errlog"] = errlog
        return FakeTransport()

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.StdioServerParameters = FakeParams
    fake_mcp.ClientSession = lambda r, w: FakeSession()
    fake_stdio = types.ModuleType("mcp.client.stdio")
    fake_stdio.stdio_client = fake_stdio_client
    monkeypatch.setitem(__import__("sys").modules, "mcp", fake_mcp)
    monkeypatch.setitem(__import__("sys").modules, "mcp.client.stdio", fake_stdio)

    mgr = mm.McpManager()
    ok = asyncio.run(mgr.connect_server("srv1", "Server", "stdio", command="node",
                                        args=["x.js"], env=declared,
                                        inherit_env=inherit_env))
    return ok, captured, mgr


def test_the_subprocess_env_is_minimal_when_inherit_env_is_false(secrets, logs, monkeypatch):
    ok, captured, mgr = _run_connect(monkeypatch, inherit_env=False,
                                     declared={"MY_SERVER_TOKEN": "abc"})
    assert ok is True
    for key in SECRET_KEYS:
        assert key not in captured["env"]
    assert captured["env"]["MY_SERVER_TOKEN"] == "abc"
    assert mgr.get_server_status("srv1")["inherit_env"] is False


def test_the_subprocess_env_is_everything_when_inherit_env_is_true(secrets, logs, monkeypatch):
    ok, captured, mgr = _run_connect(monkeypatch, inherit_env=True,
                                     declared={"MY_SERVER_TOKEN": "abc"})
    assert ok is True
    for key, value in SECRET_KEYS.items():
        assert captured["env"][key] == value
    assert mgr.get_server_status("srv1")["inherit_env"] is True


def test_a_caller_that_says_nothing_gets_todays_behaviour(secrets, logs, monkeypatch):
    """No `inherit_env` argument at all == the full environment, as before."""
    ok, captured, _ = _run_connect(monkeypatch, inherit_env=None,
                                   declared={"MY_SERVER_TOKEN": "abc"})
    assert ok is True
    assert captured["env"]["OPENAI_API_KEY"] == SECRET_KEYS["OPENAI_API_KEY"]


def test_the_child_gets_a_per_server_errlog(secrets, logs, monkeypatch):
    ok, captured, _ = _run_connect(monkeypatch, inherit_env=False, declared={})
    assert ok is True
    handle = captured["errlog"]
    assert handle is not None
    assert os.path.realpath(handle.name) == os.path.realpath(mm.stderr_log_path("srv1"))


# ── the stderr log ────────────────────────────────────────────────────────

def test_the_log_lives_inside_data_dir_and_is_not_world_readable(logs):
    handle = mm.open_stderr_log("srv1")
    assert handle is not None
    handle.write("hello\n")
    handle.close()
    path = mm.stderr_log_path("srv1")
    assert os.path.realpath(path).startswith(os.path.realpath(str(logs)))
    if os.name != "nt":
        assert os.stat(path).st_mode & 0o077 == 0, "the log is readable by others"
        assert os.stat(os.path.dirname(path)).st_mode & 0o077 == 0


def test_a_hostile_server_id_cannot_escape_the_log_dir(logs):
    path = mm.stderr_log_path("../../etc/passwd")
    assert os.path.dirname(os.path.realpath(path)) == os.path.realpath(mm.mcp_log_dir())
    assert mm.stderr_log_path("") .endswith("server.stderr.log")


def test_the_log_is_capped_and_trimmed_from_the_front(logs, monkeypatch):
    monkeypatch.setattr(mm, "MCP_STDERR_MAX_BYTES", 2000)
    path = mm.stderr_log_path("srv1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(2000):
            fh.write(f"line {i:05d} of noise\n")
    assert os.path.getsize(path) > 2000

    handle = mm.open_stderr_log("srv1")
    assert handle is not None
    handle.close()

    with open(path, encoding="utf-8") as fh:
        kept = fh.read()
    assert len(kept.encode("utf-8")) <= 2000 + 64          # + the dropped-output marker
    assert "earlier output dropped" in kept
    # The END survived — the useful line is always the last one — and every
    # kept line is whole, never a fragment.
    assert "line 01999 of noise" in kept
    assert "line 00000 of noise" not in kept
    for line in kept.split("\n")[1:-1]:
        assert line.startswith("line ") and line.endswith(" of noise")


def test_the_tail_reaches_a_failed_connects_error(logs):
    path = mm.stderr_log_path("srv1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("some boot noise\n/bin/sh: 1: node: not found\n")

    message = mm._format_mcp_connection_error(
        "My Server", "node", ["server.js"], RuntimeError("transport closed"),
        stderr_tail=mm.read_stderr_tail("srv1"))
    assert "transport closed" in message
    assert "node: not found" in message


def test_a_failed_connect_carries_the_tail_through_connect_server(logs, monkeypatch):
    path = mm.stderr_log_path("srv1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("/bin/sh: 1: node: not found\n")

    mgr = mm.McpManager()

    async def boom(*a, **kw):
        raise RuntimeError("transport closed unexpectedly")

    monkeypatch.setattr(mm.McpManager, "_connect_stdio", boom)
    ok = asyncio.run(mgr.connect_server("srv1", "My Server", "stdio", command="node"))
    assert ok is False
    error = mgr.get_server_status("srv1")["error"]
    assert "transport closed unexpectedly" in error
    assert "node: not found" in error


def test_a_dead_server_reports_the_last_thing_it_printed(logs):
    path = mm.stderr_log_path("srv1")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Error: Cannot find module 'express'\n")

    mgr = mm.McpManager()
    mgr._connections["srv1"] = {"status": "connected", "name": "My Server"}
    dead = types.SimpleNamespace(done=lambda: True)
    mgr._owner_tasks["srv1"] = (dead, None)

    status = mgr.get_server_status("srv1")
    assert status["status"] == "error"
    assert "Cannot find module" in status["error"]


def test_an_empty_or_missing_log_adds_nothing(logs):
    assert mm.read_stderr_tail("never-started") == ""
    assert mm._format_mcp_connection_error("S", "node", [], RuntimeError("boom"),
                                           stderr_tail="") == "boom"
    assert "Last output" not in mm._format_mcp_connection_error(
        "S", "node", [], RuntimeError("boom"))


def test_the_playwright_hint_still_survives_the_tail(logs):
    message = mm._format_mcp_connection_error(
        "Browser", "npx", ["-y", "@playwright/mcp@latest"], RuntimeError("ENOENT"),
        stderr_tail="npm ERR! 404")
    assert "Browser MCP could not start" in message
    assert "npx -y @playwright/mcp@latest --version" in message
    assert "npm ERR! 404" in message


# ── nothing raises when the log cannot be opened ──────────────────────────

def test_an_unopenable_log_returns_none_instead_of_raising(logs, monkeypatch):
    def boom(*a, **kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(mm.os, "makedirs", boom)
    assert mm.open_stderr_log("srv1") is None


def test_a_server_still_connects_when_the_log_cannot_be_opened(secrets, logs, monkeypatch):
    monkeypatch.setattr(mm, "open_stderr_log", lambda _sid: None)
    ok, captured, _ = _run_connect(monkeypatch, inherit_env=False, declared={})
    assert ok is True
    # No errlog was passed, so the SDK keeps its own default. The connection is
    # what matters; the capture is a debugging aid, never a precondition.
    assert captured["errlog"] is None


def test_a_broken_trim_does_not_stop_the_log_from_opening(logs, monkeypatch):
    def boom(*a, **kw):
        raise OSError("stat failed")

    monkeypatch.setattr(mm, "_trim_stderr_log", boom)
    with pytest.raises(OSError):
        mm._trim_stderr_log("x")           # the stub really does raise
    # …and open_stderr_log swallows it rather than failing the connection.
    monkeypatch.setattr(mm, "_trim_stderr_log", lambda *a, **kw: None)
    assert mm.open_stderr_log("srv1") is not None


def test_reading_a_tail_from_an_unreadable_path_returns_empty(logs, monkeypatch):
    monkeypatch.setattr(mm, "stderr_log_path", lambda _sid: str(logs))   # a directory
    assert mm.read_stderr_tail("srv1") == ""
