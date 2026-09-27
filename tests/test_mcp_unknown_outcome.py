"""A lost response must never silently repeat a dispatched MCP write."""

import asyncio
import json
from types import SimpleNamespace

from src.mcp_manager import McpManager
from src.tool_result import normalize_tool_result


def _manager(*, builtin: bool, readonly: bool):
    mgr = McpManager()
    mgr._sessions["srv"] = object()
    mgr._connections["srv"] = {"status": "connected", "name": "fixture"}
    mgr._tools["srv"] = [{"name": "action", "annotations": {"readOnlyHint": readonly}}]
    mgr.is_builtin = lambda _server_id: builtin
    mgr._stdio_owner_alive = lambda _server_id: True
    mgr._record_call_outcome = lambda *_args: None

    async def oauth_ok(_server_id):
        return None

    mgr._oauth_ensure_valid = oauth_ok
    return mgr


def test_third_party_write_lost_reply_is_unknown_and_not_replayed():
    mgr = _manager(builtin=False, readonly=False)
    calls = []

    async def applied_then_lost(_session, _name, _args):
        calls.append("applied")
        raise ConnectionError("response dropped")

    mgr._do_call = applied_then_lost
    raw = asyncio.run(mgr.call_tool("mcp__srv__action", {"value": 1}))
    assert calls == ["applied"]
    assert raw["status"] == "outcome_unknown"
    assert raw["reconcile_action"] == "read_current_state_before_retry"
    typed = normalize_tool_result(raw)
    assert typed.status == "outcome_unknown"
    assert typed.uncertainty.reconcile_action == "read_current_state_before_retry"


def test_builtin_write_reconnects_but_does_not_replay_uncertain_call():
    mgr = _manager(builtin=True, readonly=False)
    calls = []
    reconnects = []

    async def applied_then_lost(_session, _name, _args):
        calls.append("applied")
        raise ConnectionError("response dropped")

    async def reconnect(_server_id):
        reconnects.append(True)
        return True

    mgr._do_call = applied_then_lost
    mgr._reconnect_builtin = reconnect
    raw = asyncio.run(mgr.call_tool("mcp__srv__action", {}))
    assert calls == ["applied"]
    assert reconnects == [True]
    assert raw["status"] == "outcome_unknown"


def test_builtin_read_only_call_can_replay_after_reconnect():
    mgr = _manager(builtin=True, readonly=True)
    calls = []

    async def read_then_recover(_session, _name, _args):
        calls.append("read")
        if len(calls) == 1:
            raise ConnectionError("response dropped")
        return {"stdout": "current state", "exit_code": 0}

    async def reconnect(_server_id):
        return True

    mgr._do_call = read_then_recover
    mgr._reconnect_builtin = reconnect
    raw = asyncio.run(mgr.call_tool("mcp__srv__action", {}))
    assert calls == ["read", "read"]
    assert raw["stdout"] == "current state"


def test_bridge_reported_unknown_write_survives_tool_result_adapter():
    mgr = _manager(builtin=False, readonly=False)

    class Session:
        async def call_tool(self, _name, _args):
            body = {"error": "reply lost", "outcome_unknown": True,
                    "reconcile_action": "read_current_state_before_retry"}
            return SimpleNamespace(isError=True, content=[SimpleNamespace(text=json.dumps(body))])

    raw = asyncio.run(mgr._do_call(Session(), "action", {}))
    assert raw["status"] == "outcome_unknown"
    assert normalize_tool_result(raw).status == "outcome_unknown"
