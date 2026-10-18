"""Project workspace and task management through the normal owner-aware API."""
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

server = Server("project_workspace")
S = {"type": "string"}
STRINGS = {"type": "array", "items": S}
TASK = {"title": S, "body": S, "kind": S, "priority": {"type": "integer", "minimum": 0, "maximum": 3}, "owner": {"type": ["string", "null"]}, "acceptance": S, "note": S}
SPECS = {
    "project_workspace": ("POST", "/workspace/action", {"action": S, "name": S, "key": S, "kind": S, "url": S, "content": S, "digest": S, "files": {"type": "object", "additionalProperties": S}}, ["action"], False,
        "Manage project instructions, durable memory and project skills. Actions: instructions_read/write, memory_list/read/write, skills_list/catalog/repository, skill_read/write/install/import/activate/deactivate. skill_write requires the digest from skill_read. Use exact returned keys. Import never executes scripts. Activation requires an existing human review of the current digest; this tool cannot approve content."),
    "project_task_list": ("GET", "/tasks", {}, [], True, "List the project's shared Agora task ledger, also used by the person in Faustus. Objectives and acceptance belong to these tasks."),
    "project_task_get": ("GET", "/tasks/{id}", {"id": S}, ["id"], True, "Read a shared task with its full history and etag before editing."),
    "project_task_create": ("POST", "/tasks", {**TASK, "request_id": S}, ["title"], False, "Create a task in the same project board used by the person and the other agents. Acceptance criteria are text. Cite the returned id."),
    "project_task_update": ("PATCH", "/tasks/{id}", {**TASK, "id": S, "expected_etag": S}, ["id", "expected_etag"], False, "Edit task text, acceptance, priority, assignment or add a clarification. Send etag from project_task_get; stale writes are rejected. Agent workflow reviews and leases remain canonical."),
}


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=spec[5], inputSchema={"type": "object", "properties": {"project_id": S, **spec[2]}, "required": ["project_id", *spec[3]]}, annotations=ToolAnnotations(readOnlyHint=spec[4], destructiveHint=False)) for name, spec in SPECS.items()]


@server.call_tool()
async def call_tool(name, arguments):
    from urllib.parse import quote
    if name not in SPECS:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown project tool", "exit_code": 1}))]
    method, path, *_ = SPECS[name]
    args = dict(arguments or {})
    project_id = str(args.pop("project_id", ""))
    path = path.replace("{id}", quote(str(args.pop("id", "")), safe=""))
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
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            response = await client.request(method, base.rstrip("/") + "/api/projects/" + quote(project_id, safe="") + path, headers=headers, **({"params": args} if method == "GET" else {"json": args}))
        payload = response.json()
        if not response.is_success:
            payload = {"exit_code": 1, "status_code": response.status_code, "error": payload}
    except (httpx.HTTPError, ValueError, OSError) as exc:
        payload = {"exit_code": 1, "error": "Project API request failed: " + type(exc).__name__}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
