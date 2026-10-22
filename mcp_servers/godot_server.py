"""Native HTTP twin. Tool discovery never starts an app or game."""
import asyncio
import json
import os
from pathlib import Path
import sys
import uuid

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.godot.tool import SCHEMA, DESCRIPTION
from src.stdio_guard import guard

server = Server("godot")


@server.list_tools()
async def list_tools():
    return [Tool(name="godot", description=DESCRIPTION, inputSchema=SCHEMA)]


@server.call_tool()
async def call_tool(name, arguments):
    if name != "godot":
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Unknown engine tool")])
    arguments = dict(arguments or {})
    if arguments.get("action") == "operation" and not arguments.get("job_id"):
        arguments["job_id"] = uuid.uuid4().hex
    from src.constants import internal_api_base
    base = os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
    headers = {}
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    if token: headers["Authorization"] = "Bearer " + token
    if os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN"):
        headers["X-Odysseus-Internal-Token"] = os.environ["FAUSTUS_INTERNAL_TOOL_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(base.rstrip("/") + "/api/godot/execute", json=arguments, headers=headers)
        value = response.json()
        payload = value.get("structuredContent", value)
        error = not response.is_success or bool(value.get("exit_code")) or payload.get("ok") is False
    except (httpx.HTTPError, ValueError) as exc:
        payload = {"error":type(exc).__name__, "outcome_unknown":arguments.get("action") in {"operation", "run", "stop"},
                   "job_id":(arguments or {}).get("job_id"), "recovery":"Poll the original scene job_id; never replay an unconfirmed write. Debug processes remain owned by the native server."}
        error = True
    return CallToolResult(isError=error, structuredContent=payload,
                          content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))])


async def main():
    async with stdio_server() as (read, write):
        with guard(): await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__": asyncio.run(main())
