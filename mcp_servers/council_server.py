"""Native model councils through Faustus's own API."""
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("council")
S = {"type": "string"}
I = {"type": "integer"}
ID = {"session_id": S}
SPECS = {
    "council_options": ("GET", "/config", {}, [], True, "Read native room modes, role capabilities, commands, configured global Sparks route and optional budgets. Zero means unlimited."),
    "council_list": ("GET", "", {"status": S, "space_id": S}, [], True, "List the caller's persistent model council rooms, optionally in one Agora space."),
    "council_create": ("POST", "", {"title": S, "policy": S, "participants": {"type": "array", "items": {"type": "object"}}, "workspace": S, "project_id": S, "space_id": S, "budgets": {"type": "object"}}, ["participants"], False,
        "Create a native model council. Participant specs set model/endpoint_id/display_name/roles. Use the configured global route from council_options by default. Budget values0 are unlimited; positive choices remain optional."),
    "council_get": ("GET", "/{session_id}", ID, ["session_id"], True, "Read the owned room, members and current state."),
    "council_update": ("PATCH", "/{session_id}", {**ID,"changes": {"type": "object"}, "expected_revision": I}, ["session_id","changes","expected_revision"], False, "Update native room settings with the actual expected revision. Preserve existing roles, data and explicit budget choices."),
    "council_send": ("POST", "/{session_id}/messages", {**ID,"content": S,"idempotency_key": S}, ["session_id","content"], False, "Submit an instruction idempotently. Work runs independently of the MCP observer; follow the persistent room."),
    "council_command": ("POST", "/{session_id}/commands", {**ID,"command": S,"arguments": {"type": "object"}}, ["session_id","command"], False, "Use a native command from council_options (pause, resume, cancel, role/task operations). Supply existing native arguments and revision; do not invent approvals or evidence."),
    "council_messages": ("GET", "/{session_id}/messages", {**ID,"viewer_id": S,"since_id": S,"limit": I}, ["session_id"], True, "Read canonical room messages and participant-scoped discussion."),
    "council_usage": ("GET", "/{session_id}/usage", ID, ["session_id"], True, "Read actual room token/turn/round usage and optional budgets."),
    "council_ledger": ("GET", "/{session_id}/ledger", ID, ["session_id"], True, "Read native claims, observations and current ledger without creating another authority."),
    "council_tasks": ("GET", "/{session_id}/tasks", ID, ["session_id"], True, "Read native room tasks, observed results and evidence."),
    "council_decisions": ("GET", "/{session_id}/decisions", ID, ["session_id"], True, "Read persistent decisions and dissent from the native council."),
}


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=spec[5], inputSchema={"type": "object", "properties": spec[2], "required": spec[3]},
                 annotations=ToolAnnotations(readOnlyHint=spec[4], destructiveHint=False)) for name, spec in SPECS.items()]


@server.call_tool()
async def call_tool(name, arguments):
    if name not in SPECS:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown council tool", "exit_code": 1}))]
    method, path, *_ = SPECS[name]
    args = dict(arguments or {})
    if "{session_id}" in path:
        from urllib.parse import quote
        path = path.replace("{session_id}", quote(str(args.pop("session_id", "")), safe=""))
    try:
        base = os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
        headers = {}
        token = os.environ.get("FAUSTUS_TOKEN", "")
        if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        internal = os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN", "")
        if internal:
            headers["X-Odysseus-Internal-Token"] = internal
        owner = os.environ.get("FAUSTUS_COUNCIL_OWNER", "")
        if owner:
            headers["X-Faustus-Council-Owner"] = owner
        if name == "council_update":
            args["patch"] = args.pop("changes", {})
        if name == "council_command":
            args = {**(args.pop("arguments", {}) or {}), **args}
        async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
            response = await client.request(method, base.rstrip("/") + "/api/council" + path, headers=headers,
                **({"params": args} if method == "GET" else {"json": args}))
        payload = response.json()
        if not response.is_success or (isinstance(payload, dict) and payload.get("ok") is False):
            payload = {"exit_code": 1, "status_code": response.status_code, "error": payload}
    except (httpx.HTTPError, ValueError, OSError) as error:
        payload = {"exit_code": 1, "error": "Council API request failed: " + type(error).__name__}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
