"""Lote 44 (L42 point 5, mcp_manager.py half): `McpManager._reconnect_builtin`
is the crash-reconnect call site `connect_builtin_npx_server`'s own docstring
names as "today's one call site" that does not pass `owner_id`/`task_id` —
this pins that today's behaviour is DELIBERATE (a documented TODO, not an
oversight): the manager is a process-wide singleton recovering the one SHARED
builtin server from a crash, with no per-run owner/task of its own to hand
down, so the defaults stay `None` and the shared profile keeps being used,
exactly as `_npx_server_launch`'s docstring already promises for that case.

`tests/test_lote42_web03_browser_session_profile.py` already covers the
OPTIONAL owner_id/task_id path on `connect_builtin_npx_server`/
`_npx_server_launch` directly; this file only pins the one call site this
lote's report describes, not the mechanism underneath it.
"""
from __future__ import annotations

import asyncio

import pytest

import src.builtin_mcp as builtin_mcp
from src.mcp_manager import McpManager


def test_reconnect_builtin_calls_connect_with_no_owner_or_task(monkeypatch):
    """Today's hook: the reconnect loop restarts the shared server, so it
    passes neither `owner_id` nor `task_id` — asserted explicitly so a future
    change to wire a real value through has a test to update on purpose,
    instead of drifting silently."""
    mgr = McpManager()
    calls = []

    async def fake_connect(manager, server_id, *, owner_id=None, task_id=None):
        calls.append({"manager": manager, "server_id": server_id,
                      "owner_id": owner_id, "task_id": task_id})
        return True

    monkeypatch.setattr(builtin_mcp, "connect_builtin_npx_server", fake_connect)

    async def _noop_disconnect(server_id):
        return None

    monkeypatch.setattr(mgr, "disconnect_server", _noop_disconnect)

    server_id = next(iter(builtin_mcp._BUILTIN_NPX_SERVERS))
    ok = asyncio.run(mgr._reconnect_builtin(server_id))

    assert ok is True
    assert len(calls) == 1
    assert calls[0]["manager"] is mgr
    assert calls[0]["server_id"] == server_id
    # The documented gap: no owner/task context to hand down from here.
    assert calls[0]["owner_id"] is None
    assert calls[0]["task_id"] is None
