"""Personal assistant identities over the native mobile/chat API."""
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ToolAnnotations
from mcp_servers.voice_server import _request
from src.stdio_guard import guard

server = Server("personal_assistants")


@server.list_tools()
async def list_tools():
    return [Tool(name="personal_assistants", description="Configure persistent Faustus assistants: instructions, selected published skills, and native proactive schedules. Each keeps its ordinary chat/session_id, project, tools, history and voice. list includes next/last runs and recent activity; skills lists available procedures. run executes an enabled proactive mission now. Remove pauses proactive work and preserves the chat. Creation alone starts no generation.",
                 inputSchema={"type": "object", "required": ["action"], "properties": {
                     "action": {"type": "string", "enum": ["list", "create", "update", "remove", "skills", "run"]},
                     "name": {"type": "string"}, "description": {"type": "string"},
                     "session_id": {"type": "string"}, "project_id": {"type": "string"},
                     "instructions": {"type": "string"}, "skills": {"type": "array", "items": {"type": "string"}},
                     "proactivity": {"type": "object", "additionalProperties": False, "properties": {
                         "enabled": {"type": "boolean"}, "prompt": {"type": "string"},
                         "schedule": {"enum": ["hourly", "daily", "weekly", "cron"]},
                         "time": {"type": "string", "description": "HH:MM, local to timezone"},
                         "day": {"type": "integer", "minimum": 0, "maximum": 6},
                         "timezone": {"type": "string", "description": "IANA zone, e.g. Europe/Madrid"},
                         "cron": {"type": "string"}, "notifications": {"type": "boolean"}, "quiet": {"type": "boolean"}}}}},
                 annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))]


@server.call_tool()
async def call_tool(name, arguments):
    a = dict(arguments or {})
    action = a.pop("action", "list")
    method, path, body = "GET", "/assistants", None
    if name != "personal_assistants" or action not in ("list", "create", "update", "remove", "skills", "run"):
        result = {"exit_code": 1, "error": "Unknown assistant action"}
    else:
        if action == "create":
            method, body = "POST", a
        elif action == "skills":
            path += "/skills"
        elif action in ("update", "remove", "run"):
            sid = str(a.pop("session_id", ""))
            if not sid:
                return [TextContent(type="text", text=json.dumps({"exit_code": 1, "error": "session_id is required"}))]
            path += "/" + quote(sid, safe="")
            if action == "run":
                path += "/run"
            method = "PATCH" if action == "update" else "POST" if action == "run" else "DELETE"
            body = a if action == "update" else None
        try:
            async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
                result = await _request(client, method, path, api="/api/mobile", **({"json": body} if body is not None else {}))
        except httpx.HTTPError as exc:
            result = {"exit_code": 1, "error": str(exc)}
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
