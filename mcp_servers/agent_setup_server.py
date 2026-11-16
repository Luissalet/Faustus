"""agent_setup_server.py

Built-in MCP server for how agents are set up in this Faustus:

    delegation_routes_get     the named delegation routes, the default, and the
                              exact `route` choice the model is shown
    delegation_routes_set     replace them (validated as a whole)
    agent_import_preview      what Claude Code / Codex CLI / Cursor have that can be
                              imported (instructions, memories, MCP servers, skills,
                              agents), each new / changed / unchanged / exists
    agent_import_apply        import the chosen items (default: every new or changed one)
    agent_import_resync       re-read every agent imported before and import what changed
    conversations_mcp_config  how to connect Claude Code, Codex or Cursor to this
                              Faustus's conversations (mcp_servers/conversations_server.py)

A thin client of the running app's routes (/api/delegation-routes,
/api/agent-import/*, /api/conversations-mcp/config), authenticated like the
other built-in servers.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("agent_setup")

_AGENTS = {"type": "array", "items": {"type": "string", "enum": ["claude-code", "codex", "cursor"]},
           "description": "Which agents (default all three)"}
_ROUTE = {"type": "object", "properties": {
    "name": {"type": "string", "description": "What the model picks (letters, digits, . - _)"},
    "description": {"type": "string", "description": "One line the model reads to choose"},
    "model": {"type": "string"}, "endpoint_id": {"type": "string"},
    "effort": {"type": "string", "enum": ["low", "medium", "high", "max"]},
    "agent": {"type": "string", "description": "Agent definition slug (the profile: prompt, tools, permissions)"},
    "category": {"type": "string", "description": "Tool categories, comma-separated"},
    "skills": {"type": "array", "items": {"type": "string"}, "description": "Skills the worker loads first"}},
    "required": ["name", "description"]}

TOOLS = [
    Tool(name="delegation_routes_get", description="The named delegation routes for sub-agents, the default route and the "
         "`route` choice delegate_agents shows the model.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="delegation_routes_set", description="Replace the named delegation routes (and the default). Each route has a "
         "name and a one-line description (all the model sees) and what it runs on: model, endpoint, effort, agent "
         "profile, tool category, skills.",
         inputSchema={"type": "object", "properties": {"routes": {"type": "array", "items": _ROUTE},
                                                       "default": {"type": "string"}}, "required": ["routes"]}),
    Tool(name="agent_import_preview", description="List what Claude Code (~/.claude), Codex CLI (~/.codex) and Cursor "
         "(~/.cursor) have that Faustus can import: instructions (as pinned memories), memories, MCP servers, skills "
         "and agent definitions, each with its status (new, changed since the last import, unchanged, or a name that "
         "already exists here). Writes nothing.",
         inputSchema={"type": "object", "properties": {"agents": _AGENTS}}),
    Tool(name="agent_import_apply", description="Import from Claude Code / Codex CLI / Cursor. `keys` (from "
         "agent_import_preview) picks items; without it every new or changed item is imported. A changed item replaces "
         "what its earlier import created.",
         inputSchema={"type": "object", "properties": {"agents": _AGENTS,
                                                       "keys": {"type": "array", "items": {"type": "string"}}}}),
    Tool(name="agent_import_resync", description="Re-read every agent imported before and import what changed there "
         "since (and what is new). `dry_run` only lists it.",
         inputSchema={"type": "object", "properties": {"dry_run": {"type": "boolean"}}}),
    Tool(name="conversations_mcp_config", description="The client config that connects Claude Code, Codex or Cursor to "
         "this Faustus's conversations (Faustus chats and WhatsApp) as MCP tools.",
         inputSchema={"type": "object", "properties": {}}),
]


def _headers() -> dict:
    headers = {}
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        try:
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if token:
        headers["Authorization"] = "Bearer " + token
    if os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN"):
        headers["X-Odysseus-Internal-Token"] = os.environ["FAUSTUS_INTERNAL_TOOL_TOKEN"]
    return headers


def _route(name: str, a: dict):
    agents = ",".join(a.get("agents") or [])
    if name == "delegation_routes_get":
        return "GET", "/api/delegation-routes", None, None
    if name == "delegation_routes_set":
        return "PUT", "/api/delegation-routes", None, {"routes": a.get("routes") or [], "default": a.get("default") or ""}
    if name == "agent_import_preview":
        return "GET", "/api/agent-import/preview", {"agents": agents} if agents else None, None
    if name == "agent_import_apply":
        body = {"agents": a.get("agents") or []}
        if isinstance(a.get("keys"), list):
            body["keys"] = a["keys"]
        return "POST", "/api/agent-import/apply", None, body
    if name == "agent_import_resync":
        return "POST", "/api/agent-import/resync", None, {"dry_run": bool(a.get("dry_run"))}
    if name == "conversations_mcp_config":
        return "GET", "/api/conversations-mcp/config", None, None
    return None


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    spec = _route(name, dict(arguments or {}))
    if spec is None:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=f"Unknown tool: {name}")])
    method, path, params, body = spec
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            r = await client.request(method, base + path, params=params, json=body, headers=_headers())
        try:
            payload = r.json()
        except ValueError:
            payload = {"text": r.text[:1000]}
        error = not r.is_success
    except httpx.HTTPError as exc:
        payload, error = {"error": f"Faustus did not answer ({type(exc).__name__})"}, True
    return CallToolResult(isError=error, content=[TextContent(
        type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
