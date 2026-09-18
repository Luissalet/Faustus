"""The Context Engine MCP server (mcp_servers/context_engine_server.py).

Three things are pinned here, and each is a real failure mode:

* importing the module must not start the engine. `_ensure_init()` reaches the
  adapter registry and through it memory, RAG and the provenance graph; paying
  that at import would put a second on a server that is usually only asked to
  list its tools;
* `list_tools()` must return all eight with a schema a client can actually
  validate against — a tool whose `inputSchema` is not an object with typed
  properties is a tool the model guesses at;
* a write with no `ODYSSEUS_MCP_CONTEXT_OWNER` must be refused and must say so
  in a sentence naming the variable. A block written into the wrong owner's
  scope is a sentence pasted into somebody else's prompts, and nothing
  afterwards can tell that it was not theirs.
"""

import asyncio

import pytest

pytest.importorskip("mcp")

import mcp_servers.context_engine_server as ces

TOOL_NAMES = {
    "context_compile", "context_explain", "context_blocks", "context_capsule",
    "context_experiences", "context_code_index", "context_findings",
    "context_diagnostics",
}


@pytest.fixture(autouse=True)
def _clear_owner_env(monkeypatch):
    for key in ces._OWNER_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _call(name, arguments):
    return asyncio.run(ces.call_tool(name, arguments))


def test_importing_the_server_starts_nothing(monkeypatch):
    """The module is imported at the top of this file; if that had initialised
    the engine, `_initialized` would already be True and the adapter registry
    would have been built before any client connected."""
    assert ces._initialized is False
    assert ces._engine == {}
    assert ces.server.name == "context"


def test_list_tools_returns_eight_usable_schemas():
    tools = asyncio.run(ces.list_tools())
    assert {t.name for t in tools} == TOOL_NAMES

    for tool in tools:
        assert tool.description and len(tool.description) > 80, tool.name
        schema = tool.inputSchema
        assert schema["type"] == "object", tool.name
        properties = schema.get("properties") or {}
        assert properties, tool.name
        for field, spec in properties.items():
            assert isinstance(spec, dict) and "type" in spec, f"{tool.name}.{field}"
        for required in schema.get("required", []):
            assert required in properties, f"{tool.name}.{required}"


def test_a_write_without_an_owner_is_refused_and_says_which_variable():
    """And it is refused BEFORE the engine is loaded: the check is on the
    action, not on what the store says afterwards."""
    out = _call("context_blocks", {"action": "create", "type": "project_rules",
                                   "title": "Rules", "content": "Keep it simple."})
    assert "ODYSSEUS_MCP_CONTEXT_OWNER" in out[0].text
    assert "create" in out[0].text
    assert ces._initialized is False

    for name, arguments in (
        ("context_capsule", {"action": "apply", "scope_id": "s1", "deltas": []}),
        ("context_findings", {"action": "post", "scope": "run-1", "claim": "x"}),
        ("context_code_index", {"action": "refresh", "workspace": "/repo"}),
    ):
        refused = _call(name, arguments)
        assert "ODYSSEUS_MCP_CONTEXT_OWNER" in refused[0].text, name


def test_an_unknown_tool_is_a_message_not_an_exception():
    """An exception out of call_tool kills the session, and a dead session
    takes every other tool on this server with it."""
    out = _call("context_nope", {})
    assert "Unknown tool" in out[0].text
