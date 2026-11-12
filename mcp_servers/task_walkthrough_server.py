"""Task walkthrough MCP client of the running Faustus HTTP surface."""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.constants import internal_api_base
from src.stdio_guard import guard
from src.task_walkthrough import SCHEMA
from mcp_servers.app_dock_server import _headers

server = Server("task_walkthrough")


@server.list_tools()
async def list_tools():
    return [Tool(name=SCHEMA["name"], description=SCHEMA["description"], inputSchema=SCHEMA["parameters"])]


@server.call_tool()
async def call_tool(name, arguments):
    if name != "task_walkthrough":
        raise ValueError("Unknown tool")
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            response = await client.post(base + "/api/task-walkthrough", json=arguments, headers=_headers())
            if not response.is_success:
                raise ValueError(response.text)
            return [TextContent(type="text", text=response.text)]
    except httpx.HTTPError as exc:
        raise ValueError(str(exc)) from exc


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
