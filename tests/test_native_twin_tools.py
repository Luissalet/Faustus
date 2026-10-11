"""MCP copies of native tools are not offered to the agent twice, and the list of single-tool copies stays true."""

import asyncio
import importlib

import pytest

from src import builtin_mcp
from src.tool_footprint import native_twin
from src.tool_index import _is_native_twin
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

NATIVE = {t["function"]["name"]: t["function"] for t in FUNCTION_TOOL_SCHEMAS}


def _server_tools(server: str) -> dict:
    script, _ = builtin_mcp._BUILTIN_SERVERS[server]
    module = importlib.import_module(script[:-3].replace("/", "."))
    return {t.name: t for t in asyncio.run(module.list_tools())}


@pytest.mark.parametrize("server", sorted(builtin_mcp.NATIVE_TWIN_TOOLS))
def test_listed_copies_take_nothing_the_native_tool_does_not(server):
    tools = _server_tools(server)
    for name in builtin_mcp.NATIVE_TWIN_TOOLS[server]:
        assert name in tools, f"{server} no longer publishes {name}"
        assert name in NATIVE, f"{name} has no native tool any more"
        mcp_params = set((tools[name].inputSchema or {}).get("properties", {}))
        native_params = set((NATIVE[name].get("parameters") or {}).get("properties", {}))
        assert mcp_params <= native_params, f"{server}.{name} accepts {sorted(mcp_params - native_params)} the native one does not"


def test_copies_that_do_more_stay_offered():
    email = _server_tools("email")
    for name in ("send_email", "reply_to_email", "read_email"):
        extra = set((email[name].inputSchema or {}).get("properties", {})) - set(NATIVE[name]["parameters"]["properties"])
        assert extra, f"email.{name} no longer does more than the native tool: list it in NATIVE_TWIN_TOOLS"
        assert not builtin_mcp.is_native_twin(f"mcp__email__{name}")
    assert not builtin_mcp.is_native_twin("mcp__memory__manage_memory")


def test_index_and_footprint_apply_the_same_rule():
    for name, hidden in [("mcp__email__list_emails", True), ("mcp__email__send_email", False),
                         ("mcp__image_gen__generate_image", True), ("mcp__context__context_recall", True),
                         ("mcp__context__context_compile", False), ("mcp__harness__doc_lint", True),
                         ("mcp__harness__tool_footprint", False), ("mcp__code_graph__anything", True),
                         ("list_emails", False), ("mcp__public_data__earthquakes", False)]:
        assert builtin_mcp.is_native_twin(name) is hidden, name
        assert _is_native_twin(name) is hidden and native_twin(name) is hidden, name
