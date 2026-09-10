"""Lot 36, requirement 3 (L24/L29): builtin-browser MCP action tools
(click/type/navigate/select/press/…) now go through
`src.browser_actions.run_with_precondition` — a FRESH `browser_snapshot`
right before the call, checked against `ref`/`expected_url` when the model
passed them, with a readback (URL/title/dom hash) always attached.

`browser_actions.py` itself was already built and tested in isolation by an
earlier lot (its own docstring says the wiring belongs elsewhere); this file
tests the WIRING in `src/tool_execution.py`, with the MCP manager mocked —
no real browser or network.
"""
from __future__ import annotations

import json

import pytest

import src.tool_execution as tool_execution
from src.agent_tools import ToolBlock


def _block(tool_type: str, args: dict) -> ToolBlock:
    return ToolBlock(tool_type=tool_type, content=json.dumps(args))


class _FakeMcp:
    """Records every call_tool invocation and answers from a small script:
    a `browser_snapshot` reply, then whatever the real action should return.
    """

    def __init__(self, snapshot_text: str, action_result: dict):
        self.calls: list[tuple[str, dict]] = []
        self._snapshot_text = snapshot_text
        self._action_result = action_result

    async def call_tool(self, tool: str, args: dict):
        self.calls.append((tool, dict(args)))
        if tool == "mcp__builtin_browser__browser_snapshot":
            return {"stdout": self._snapshot_text, "exit_code": 0}
        return dict(self._action_result)


SNAPSHOT_WITH_REF = (
    "- Page URL: https://example.com/cart\n"
    "- Page Title: Cart\n"
    "- button \"Checkout\" [ref=e7]\n"
)


@pytest.mark.asyncio
async def test_browser_click_snapshots_first_and_attaches_a_readback(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp(SNAPSHOT_WITH_REF, {"stdout": "Clicked", "exit_code": 0})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"}),
        session_id="s1", owner="alice",
    )

    # snapshot, then the real click, then a second snapshot for the readback.
    assert [c[0] for c in mcp.calls] == [
        "mcp__builtin_browser__browser_snapshot",
        "mcp__builtin_browser__browser_click",
        "mcp__builtin_browser__browser_snapshot",
    ]
    assert mcp.calls[1][1] == {"element": "Checkout", "ref": "e7"}
    assert result["stdout"] == "Clicked"
    assert result["readback"]["url"] == "https://example.com/cart"
    assert result["readback"]["title"] == "Cart"
    assert result["readback"]["dom_hash"]


@pytest.mark.asyncio
async def test_stale_ref_blocks_the_click_before_it_runs(monkeypatch):
    """WEB-04's own acceptance criterion: a layout shift that dropped the
    referenced element must refuse the click instead of clicking whatever is
    now at that reference."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp(SNAPSHOT_WITH_REF, {"stdout": "Clicked", "exit_code": 0})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click", {"element": "Delete", "ref": "e999-gone"}),
        session_id="s1", owner="alice",
    )

    assert result["blocked"] is True
    assert "e999-gone" in result["error"]
    # Only the precondition snapshot ran — never the click.
    assert [c[0] for c in mcp.calls] == ["mcp__builtin_browser__browser_snapshot"]


@pytest.mark.asyncio
async def test_expected_url_mismatch_blocks_and_is_stripped_from_the_forwarded_call(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp(SNAPSHOT_WITH_REF, {"stdout": "Clicked", "exit_code": 0})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click",
               {"element": "Checkout", "ref": "e7", "expected_url": "/checkout"}),
        session_id="s1", owner="alice",
    )
    assert result["blocked"] is True
    assert "/checkout" in result["error"]


@pytest.mark.asyncio
async def test_expected_url_match_forwards_the_call_without_the_extra_key(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp(SNAPSHOT_WITH_REF, {"stdout": "Clicked", "exit_code": 0})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_click",
               {"element": "Checkout", "ref": "e7", "expected_url": "/cart"}),
        session_id="s1", owner="alice",
    )
    assert "blocked" not in result
    # `expected_url` is Faustus's own addition, never part of the real
    # Playwright MCP schema — it must not leak into the forwarded call.
    assert mcp.calls[1] == ("mcp__builtin_browser__browser_click", {"element": "Checkout", "ref": "e7"})


@pytest.mark.asyncio
async def test_non_action_browser_tools_are_not_wrapped(monkeypatch):
    """A pure read (browser_snapshot itself, console messages, …) is not an
    `is_browser_action` — no precondition, no extra round trip, exactly the
    single call it always made."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp(SNAPSHOT_WITH_REF, {"stdout": "console log", "exit_code": 0})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_console_messages", {}),
        session_id="s1", owner="alice",
    )
    assert "readback" not in result
    assert [c[0] for c in mcp.calls] == ["mcp__builtin_browser__browser_console_messages"]


@pytest.mark.asyncio
async def test_action_with_neither_ref_nor_expected_url_still_runs_and_gets_a_readback(monkeypatch):
    """No capability lost (COMUN.md rule 3): the overwhelming majority of
    real calls name neither field — same as before this lot, just with a
    readback attached now."""
    monkeypatch.setenv("AUTH_ENABLED", "false")
    mcp = _FakeMcp(SNAPSHOT_WITH_REF, {"stdout": "Navigated", "exit_code": 0})
    monkeypatch.setattr(tool_execution, "get_mcp_manager", lambda: mcp)

    desc, result = await tool_execution._execute_tool_block_impl(
        _block("mcp__builtin_browser__browser_navigate", {"url": "https://example.com/cart"}),
        session_id="s1", owner="alice",
    )
    assert result["stdout"] == "Navigated"
    assert result["readback"]["url"] == "https://example.com/cart"
