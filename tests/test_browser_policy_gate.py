"""Audit finding 6 (policy half): a browser-specific read/action split under
the external-context approval gate.

Observation tools (navigate, snapshot, screenshot, find, wait_for, console,
network, tabs list) are reads for the gate's purposes; every interaction
(click, type, fill_form, …, evaluate, run_code_unsafe, mouse_*_xy) stays
gated once untrusted content has entered the run.
"""

import json

import pytest

from src.tool_capabilities import (
    BROWSER_ACTION_TOOLS,
    BROWSER_CODE_EXECUTION_TOOLS,
    BROWSER_MCP_PREFIX,
    BROWSER_READ_TOOLS,
    ToolEffect,
    ToolRunSecurityContext,
    capabilities_for_action,
    capabilities_for_tool,
)


P = BROWSER_MCP_PREFIX


def _tainted():
    return ToolRunSecurityContext(external_untrusted_context_seen=True)


def test_read_tool_list_matches_the_spec():
    assert BROWSER_READ_TOOLS == {
        "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_take_screenshot",
        "browser_find", "browser_wait_for", "browser_console_messages", "browser_network_requests",
        "browser_network_request",
    }
    assert BROWSER_CODE_EXECUTION_TOOLS == {"browser_evaluate", "browser_run_code_unsafe"}
    assert not (BROWSER_READ_TOOLS & BROWSER_ACTION_TOOLS)
    assert not (BROWSER_READ_TOOLS & BROWSER_CODE_EXECUTION_TOOLS)


@pytest.mark.parametrize("name", sorted(BROWSER_READ_TOOLS))
def test_read_tools_are_auto_approved_after_external_context(name):
    caps = capabilities_for_tool(P + name)
    assert caps.known is True
    assert caps.effects == {ToolEffect.BROKERED_NETWORK_READ}
    assert _tainted().decision_for(P + name, '{"url": "https://example.com"}').allowed is True


@pytest.mark.parametrize("name", sorted((BROWSER_ACTION_TOOLS - {"browser_tabs"}) | BROWSER_CODE_EXECUTION_TOOLS))
def test_action_and_code_tools_stay_gated(name):
    decision = _tainted().decision_for(P + name, '{"element": "x", "target": "e1"}')
    assert decision.allowed is False
    assert "unknown/high-impact" in decision.reason


def test_tabs_is_a_read_only_for_list():
    tabs = P + "browser_tabs"
    assert _tainted().decision_for(tabs, '{"action": "list"}').allowed is True
    assert _tainted().decision_for(tabs, {"action": "list"}).allowed is True
    for action in ("new", "close", "select"):
        assert _tainted().decision_for(tabs, json.dumps({"action": action, "index": 0})).allowed is False
    # no action at all: fail closed
    assert _tainted().decision_for(tabs, "{}").allowed is False
    assert capabilities_for_action(tabs, '{"action": "list"}').known is True


def test_browser_results_still_arm_the_gate():
    ctx = ToolRunSecurityContext()
    ctx.observe_tool_result(P + "browser_navigate", {"stdout": "### Page\n- Page URL: https://x", "exit_code": 0})
    assert ctx.external_untrusted_context_seen is True
    assert ctx.decision_for(P + "browser_snapshot").allowed is True
    assert ctx.decision_for(P + "browser_fill_form", "{}").allowed is False
