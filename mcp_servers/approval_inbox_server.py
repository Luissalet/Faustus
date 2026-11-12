"""Read the running Faustus approval inbox through the same HTTP API."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
from mcp_servers.app_dock_server import _headers
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("approval_inbox")


@server.list_tools()
async def list_tools():
    return [Tool(name="approval_inbox", description="Read existing pending approval codes, levels, age, origin and remembered exact patterns. Does not create or decide approvals.",
                 inputSchema={"type": "object", "properties": {}, "additionalProperties": False})]


@server.call_tool()
async def call_tool(name, arguments):
    if name != "approval_inbox":
        raise ValueError("Unknown tool")
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
        response = await client.get(base + "/api/approval-inbox", headers=_headers())
        response.raise_for_status()
        return [TextContent(type="text", text=json.dumps(response.json(), ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
