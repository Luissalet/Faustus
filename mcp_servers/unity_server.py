"""Unity MCP twin of the native agent operation; discovery starts nothing."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.stdio_guard import guard as stdout_guard
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from src.unity.tool import DESCRIPTION, SCHEMA, UnityTool

server = Server("unity")


@server.list_tools()
async def list_tools():
    return [Tool(name="unity", description=DESCRIPTION, inputSchema=SCHEMA)]


@server.call_tool()
async def call_tool(name, arguments):
    if name != "unity":
        return CallToolResult(content=[TextContent(type="text", text="Unknown Unity tool")], isError=True)
    result = await UnityTool().execute(json.dumps(arguments))
    return CallToolResult(content=[TextContent(type="text", text=result["output"])],
                          structuredContent=result.get("structuredContent"), isError=bool(result.get("exit_code")))


async def run():
    async with stdio_server() as (read, write):
        with stdout_guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
