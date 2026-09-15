"""`lookup_tools` — on-demand catalog so the agent can load schemas without
dumping every tool into the window."""
import asyncio
import json

from src.agent_tools import ToolBlock, TOOL_HANDLERS, TOOL_TAGS
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
from src.tool_security import PLAN_MODE_READONLY_TOOLS, is_public_blocked_tool
from src.tool_serve import LOOKUP_TOOL


def _run(content, **kwargs):
    return asyncio.run(execute_tool_block(
        ToolBlock(LOOKUP_TOOL, content),
        security_context=NO_TOOL_SECURITY_CONTEXT,
        **kwargs,
    ))


def test_registered_everywhere():
    assert LOOKUP_TOOL in TOOL_TAGS
    assert LOOKUP_TOOL in TOOL_HANDLERS
    assert LOOKUP_TOOL in ALWAYS_AVAILABLE
    assert LOOKUP_TOOL in BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert LOOKUP_TOOL in names
    assert is_public_blocked_tool(LOOKUP_TOOL) is False
    assert LOOKUP_TOOL in PLAN_MODE_READONLY_TOOLS


def test_execute_loads_named_tool_schema():
    _, result = _run(json.dumps({"names": ["ask_user"], "detail": "schema"}))
    assert result.get("exit_code") == 0
    assert "ask_user" in result["promote"]
    payload = result[LOOKUP_TOOL]
    assert payload["tools"][0]["schema"]["function"]["name"] == "ask_user"


def test_disabled_tools_are_not_served():
    _, result = _run(
        json.dumps({"names": ["ask_user", "send_email"]}),
        disabled_tools={"send_email"},
    )
    assert "send_email" not in result["promote"]
    assert "ask_user" in result["promote"]
