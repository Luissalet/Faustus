"""The research_prune MCP server (mcp_servers/research_prune_server.py): same
discipline as the other built-in servers -- in-process, no stdio transport."""
import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp_servers.research_prune_server as srv

FIXTURES = Path(__file__).parent / "fixtures" / "research_pages"


def _call(name, arguments):
    return asyncio.run(srv.call_tool(name, arguments))


def test_server_identity_and_tool_surface():
    assert srv.server.name == "research_prune"
    tools = asyncio.run(srv.list_tools())
    assert [t.name for t in tools] == ["page_prune"]
    schema = tools[0].inputSchema
    assert schema["required"] == ["query"]
    assert len(tools[0].description) > 60
    for required in schema["required"]:
        assert required in schema["properties"]


def test_prunes_html_through_the_server():
    html = (FIXTURES / "rate_limit_en.html").read_text(encoding="utf-8")
    out = _call("page_prune", {"query": "token bucket burst", "html": html})
    payload = json.loads(out[0].text)
    assert payload["ok"] is True
    assert payload["stats"]["pruned_chars"] < payload["stats"]["original_chars"]
    assert "token bucket" in payload["output"].lower()


def test_errors_are_messages_not_exceptions():
    assert "Unknown tool" in _call("nope", {})[0].text
    assert "provide a query" in _call("page_prune", {"url": "https://x.test"})[0].text
    assert "provide one of url, html or text" in _call("page_prune", {"query": "q"})[0].text
    assert "provide a query" in asyncio.run(srv.call_tool("page_prune", None))[0].text


def test_registered_as_a_builtin_native_twin_server():
    from src import builtin_mcp

    script, name = builtin_mcp._BUILTIN_SERVERS["research_prune"]
    assert script == "mcp_servers/research_prune_server.py"
    assert (Path(__file__).parent.parent / script).is_file()
    assert "research_prune" in builtin_mcp.NATIVE_TWIN_SERVERS
