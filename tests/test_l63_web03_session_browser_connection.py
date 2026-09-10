"""L63 · WEB-03 — a per-session browser MCP PROCESS, not just a per-session
profile directory (docs/spec/v2/backlog.json WEB-03).

`src/browser_sessions.py` (lote 42/44) already gave every `(owner_id,
task_id)` its own on-disk profile directory, and `_browser_mcp_args`/
`_npx_server_launch` already resolved it when given both -- but every real
caller still registered the resulting connection under the single shared
`BROWSER_SERVER_ID` on `McpManager`, so two sessions launched one after the
other would still collide on the SAME live connection (last one to connect
wins). `connect_session_browser`/`disconnect_session_browser` close that:
each session gets its OWN `McpManager` connection id
(`session_browser_server_id`), so two sessions' browser processes can be
connected on the same manager at once without either one being able to see
the other's cookies/login state, and closing one task's session never
touches the single shared `BROWSER_SERVER_ID` or its persistent profile
directory.

`_npx_server_launch(server_id)` -- the one-positional-argument call several
existing tests pin (e.g. `tests/test_browser_mcp_reconnect.py`'s
`monkeypatch.setattr(builtin_mcp, "_npx_server_launch", lambda sid: (...))`)
-- is untouched: this lote adds new functions, it does not change that one's
signature or behaviour.
"""
from __future__ import annotations

import asyncio

import pytest

import src.builtin_mcp as builtin_mcp
from src import browser_sessions as bs


@pytest.fixture(autouse=True)
def _isolated_sessions(tmp_path, monkeypatch):
    import src.constants as constants
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", "/usr/bin/chromium")
    bs._SESSIONS.clear()
    yield
    bs._SESSIONS.clear()


class _FakeManager:
    def __init__(self):
        self.connect_calls = []
        self.disconnect_calls = []
        self._connections = {}
        self._meta = {}

    async def connect_server(self, *, server_id, name, transport, command=None,
                             args=None, env=None, url=None, inherit_env=None):
        self.connect_calls.append({
            "server_id": server_id, "name": name, "args": list(args or []),
        })
        self._connections[server_id] = {"status": "connected", "name": name}
        return True

    async def disconnect_server(self, server_id):
        self.disconnect_calls.append(server_id)
        self._connections.pop(server_id, None)

    def set_connection_meta(self, server_id, **meta):
        self._meta[server_id] = meta


def test_connect_session_browser_uses_a_session_scoped_server_id_not_the_shared_one():
    mgr = _FakeManager()
    ok, server_id = asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    assert ok is True
    assert server_id != builtin_mcp.BROWSER_SERVER_ID
    assert server_id.startswith(builtin_mcp.BROWSER_SERVER_ID + ":")
    assert mgr.connect_calls[0]["server_id"] == server_id


def test_connect_session_browser_launches_with_that_sessions_own_profile_dir():
    mgr = _FakeManager()
    asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    args = mgr.connect_calls[0]["args"]
    expected_dir = bs.get_session("alice", "task-1").profile_dir
    assert "--user-data-dir" in args
    assert args[args.index("--user-data-dir") + 1] == expected_dir
    assert expected_dir != builtin_mcp._browser_profile_dir()


def test_two_sessions_get_two_different_server_ids_and_can_coexist():
    """The literal WEB-03 acceptance: one project must not see -- or step on
    -- another project's authenticated browser session."""
    mgr = _FakeManager()
    ok_a, id_a = asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    ok_b, id_b = asyncio.run(builtin_mcp.connect_session_browser(mgr, "bob", "task-2"))
    assert ok_a is True and ok_b is True
    assert id_a != id_b
    # Both connections are live on the manager at once -- neither replaced
    # the other, unlike two callers both registering under BROWSER_SERVER_ID.
    assert id_a in mgr._connections and id_b in mgr._connections

    dir_a = bs.get_session("alice", "task-1").profile_dir
    dir_b = bs.get_session("bob", "task-2").profile_dir
    assert dir_a != dir_b


def test_session_browser_server_id_is_deterministic_and_owner_task_scoped():
    a1 = builtin_mcp.session_browser_server_id("alice", "task-1")
    a1_again = builtin_mcp.session_browser_server_id("alice", "task-1")
    a2 = builtin_mcp.session_browser_server_id("alice", "task-2")
    b1 = builtin_mcp.session_browser_server_id("bob", "task-1")
    assert a1 == a1_again
    assert len({a1, a2, b1}) == 3


# ── closing a task cleans up only that task's session ───────────────────────

def test_disconnect_session_browser_only_touches_that_sessions_connection():
    mgr = _FakeManager()
    _, id_a = asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    _, id_b = asyncio.run(builtin_mcp.connect_session_browser(mgr, "bob", "task-2"))

    closed = asyncio.run(builtin_mcp.disconnect_session_browser(mgr, "alice", "task-1"))
    assert closed is True
    assert mgr.disconnect_calls == [id_a]
    assert id_a not in mgr._connections
    # Bob's own session/connection is completely untouched.
    assert id_b in mgr._connections
    assert bs.get_session("bob", "task-2").is_open is True


def test_disconnect_session_browser_never_touches_the_shared_server_id():
    mgr = _FakeManager()
    mgr._connections[builtin_mcp.BROWSER_SERVER_ID] = {"status": "connected", "name": "shared"}
    asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    asyncio.run(builtin_mcp.disconnect_session_browser(mgr, "alice", "task-1"))
    assert builtin_mcp.BROWSER_SERVER_ID not in mgr.disconnect_calls
    assert builtin_mcp.BROWSER_SERVER_ID in mgr._connections  # untouched


def test_closing_a_task_deletes_that_tasks_profile_but_never_the_shared_persistent_one(tmp_path):
    mgr = _FakeManager()
    asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    session_dir = bs.get_session("alice", "task-1").profile_dir
    assert session_dir.startswith(bs._sessions_root())

    asyncio.run(builtin_mcp.disconnect_session_browser(mgr, "alice", "task-1"))

    import os
    assert not os.path.isdir(session_dir)
    # The single shared persistent profile lives under a sibling path this
    # function never references at all.
    shared_dir = builtin_mcp._browser_profile_dir()
    assert shared_dir != session_dir
    assert "browser-sessions" not in shared_dir


def test_disconnect_without_delete_profile_keeps_the_directory_but_closes_the_session():
    mgr = _FakeManager()
    asyncio.run(builtin_mcp.connect_session_browser(mgr, "alice", "task-1"))
    session_dir = bs.get_session("alice", "task-1").profile_dir

    asyncio.run(builtin_mcp.disconnect_session_browser(mgr, "alice", "task-1", delete_profile=False))

    import os
    assert os.path.isdir(session_dir)
    assert bs.get_session("alice", "task-1").is_open is False


# ── the pinned single-argument call path is untouched ───────────────────────

def test_npx_server_launch_single_arg_path_still_works(monkeypatch):
    args, env = builtin_mcp._npx_server_launch(builtin_mcp.BROWSER_SERVER_ID)
    assert "--user-data-dir" in args
    assert args[args.index("--user-data-dir") + 1] == builtin_mcp._browser_profile_dir()
    assert bs._SESSIONS == {}  # the single-arg path never opens a session
