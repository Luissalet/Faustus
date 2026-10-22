"""Conversation objectives over Faustus's normal local HTTP API. Starts no app."""
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("conversation_objective")
SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["get", "save", "state", "continue"]},
    "session_id": {"type": "string"}, "expected_revision": {"type": "integer"},
    "selected_model": {"type": "string"}, "selected_endpoint_id": {"type": "string"}, "selected_endpoint_url": {"type": "string"}, "workspace": {"type": "string"},
    "brief": {"type": "object"}, "status": {"type": "string", "enum": ["active", "paused", "complete"]},
    "result": {"type": "string"}}, "required": ["action", "session_id"], "additionalProperties": False}


@server.list_tools()
async def list_tools():
    return [Tool(name="conversation_objective", description="Read/edit the saved conversation objective, context, deliverable and native references. Use expected_revision from get for writes. Pausing requests a safe-point pause of the current generation; schedules keep their own state. Continue rejoins an active run or starts one idempotent continuation from saved history, without replaying tool effects. Transport completion never completes the objective. References: task(project_id,id), agent(child session id), schedule(task id), resource(workspace,path), change(repo id,workspace,path,scope staged|unstaged).", inputSchema=SCHEMA)]


@server.call_tool()
async def call_tool(name, arguments):
    args = dict(arguments or {})
    action = args.pop("action", "")
    sid = args.pop("session_id", "")
    routes = {"get": ("GET", ""), "save": ("PUT", ""), "state": ("POST", "/state"), "continue": ("POST", "/continue")}
    if name != "conversation_objective" or action not in routes or not isinstance(sid, str) or not sid:
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Invalid conversation objective request")])
    method, suffix = routes[action]
    base = os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
    headers = {}
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    if os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN"):
        headers["X-Odysseus-Internal-Token"] = os.environ["FAUSTUS_INTERNAL_TOOL_TOKEN"]
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.request(method, base.rstrip("/") + "/api/session/" + quote(sid, safe="") + "/objective" + suffix,
                                            headers=headers, **({} if method == "GET" else {"json": args}))
        payload = response.json()
        error = not response.is_success
        if error:
            payload = {"error": payload, "status_code": response.status_code}
    except (httpx.HTTPError, ValueError) as exc:
        payload = {"error": type(exc).__name__, "outcome_unknown": method != "GET",
                   "recovery": "Read the objective and run status. Retry Continue only with the original expected_revision."}
        error = True
    return CallToolResult(isError=error, structuredContent=payload,
                          content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
