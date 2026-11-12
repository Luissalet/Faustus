"""Connected-data research through the running Faustus research API."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult
from src.constants import internal_api_base
from src.stdio_guard import guard
from mcp_servers.app_dock_server import _headers

server = Server("connected_research")
_QUERY = {"query": {"type": "string"}, "source_ids": {"type": "array", "items": {"type": "string"}},
          "source_filters": {"type": "object"}, "clarification": {"type": "string"},
          "max_rounds": {"type": "integer", "minimum": 1, "maximum": 20}, "model": {"type": "string"}, "endpoint_id": {"type": "string"},
          "effort": {"type": "string", "enum": ["off", "low", "medium", "high", "max"]}}
_TOOLS = [
    Tool(name="connected_research_sources", description="List the owner's existing research sources, search lanes and supported filters. No ingestion or web search.", inputSchema={"type": "object", "properties": {}}),
    Tool(name="connected_research_plan", description="Prepare a visible cross-source research plan and an optional clarification before searching private data.", inputSchema={"type": "object", "properties": _QUERY, "required": ["query"]}),
    Tool(name="connected_research_start", description="Research only existing connected data, using each app's own tools. Returns a run id. Reports require citations opening the original; no web fallback.", inputSchema={"type": "object", "properties": _QUERY, "required": ["query"]}),
    Tool(name="connected_research_status", description="Read a research run's live source timeline and plan.", inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]}),
    Tool(name="connected_research_result", description="Read a saved connected-data report, citations and the source timeline without consuming it.", inputSchema={"type": "object", "properties": {"session_id": {"type": "string"}}, "required": ["session_id"]}),
]


@server.list_tools()
async def list_tools():
    return _TOOLS


@server.call_tool()
async def call_tool(name, arguments):
    from urllib.parse import quote
    args = dict(arguments or {})
    base = (os.getenv("FAUSTUS_INTERNAL_API_URL") or os.getenv("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    headers = _headers()
    owner = os.getenv("ODYSSEUS_MCP_RESEARCH_OWNER", "")
    if owner:
        headers["X-Odysseus-Owner"] = owner
    try:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            if name == "connected_research_sources":
                response = await client.get(base + "/api/research/connected-sources", headers=headers)
            elif name in ("connected_research_plan", "connected_research_start"):
                path = "/api/research/connected-plan" if name.endswith("plan") else "/api/research/start"
                response = await client.post(base + path, headers=headers, json={**args, "data_scope": "connected"})
            elif name in ("connected_research_status", "connected_research_result"):
                sid = quote(str(args.get("session_id") or ""), safe="")
                if name.endswith("status"):
                    response = await client.get(base + "/api/research/status/" + sid, headers=headers)
                else:
                    response = await client.post(base + "/api/research/result-peek/" + sid, headers=headers)
            else:
                raise ValueError("Unknown research tool")
            return CallToolResult(isError=not response.is_success, content=[TextContent(type="text", text=response.text)])
    except Exception as exc:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=str(exc))])


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
