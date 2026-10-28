"""Exercise the actual manager discovery methods without booting app/database.

Only the methods under test are compiled from the production class. Transport
is covered separately by a real stdio server; this test has no model or DB.
"""
import ast
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parents[2]


def manager():
    tree = ast.parse((ROOT / "src/mcp_manager.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "McpManager")
    names = {"is_builtin", "get_all_openai_schemas", "get_all_tools", "_grouped_prompt_tools"}
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(methods) == len(names)
    namespace = dict(Dict=Dict, List=List, Optional=Optional,
                     BROWSER_MCP_SERVER_ID="builtin_browser",
                     builtin_browser_policy_disabled=lambda: set(),
                     _browser_tool_denied=lambda name, denied: False)
    exec(compile(ast.Module(body=methods, type_ignores=[]), "manager-discovery", "exec"), namespace)
    subset = type("ManagerDiscovery", (), {name: namespace[name] for name in names})()
    schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    subset._tools = {
        server: [{"name": tool, "description": "Existing browser session", "input_schema": schema}]
        for server, tool in [("browser_session", "browser_session"), ("builtin_browser", "browser_snapshot"),
                             ("email", "list_emails"), ("external", "lookup")]
    }
    subset._connections = {server: {"name": server} for server in subset._tools}
    return subset


def test_real_session_is_visible_to_function_and_text_models():
    subject = manager()
    assert subject.is_builtin("browser_session")
    assert not subject.is_builtin("external")
    schemas = subject.get_all_openai_schemas()
    assert {entry["function"]["name"] for entry in schemas} == {
        "mcp__browser_session__browser_session", "mcp__builtin_browser__browser_snapshot", "mcp__external__lookup"}
    grouped = subject._grouped_prompt_tools()
    assert set(grouped) == {"browser_session", "builtin_browser", "external"}
    assert grouped["browser_session"][0]["qualified_name"] == "mcp__browser_session__browser_session"


def test_existing_disabled_map_applies_to_both_catalogues():
    subject = manager()
    disabled = {"browser_session": {"browser_session"}}
    assert all(entry["function"]["name"] != "mcp__browser_session__browser_session"
               for entry in subject.get_all_openai_schemas(disabled))
    assert "browser_session" not in subject._grouped_prompt_tools(disabled)
    assert "builtin_browser" in subject._grouped_prompt_tools(disabled)
