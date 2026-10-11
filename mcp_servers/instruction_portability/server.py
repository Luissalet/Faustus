"""Built-in MCP bridge to portable instructions and advisory Markdown hooks."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.stdio_guard import guard as stdout_guard

server = Server("instruction_portability")
_BASE = "/api/instruction-portability"
_EXPORT = {"target": {"type": "string", "enum": ["claude", "codex", "cursor"]}, "workspace": {"type": "string"}, "destination": {"type": "string"},
           **{key: {"type": "array", "items": {"type": "string"}} for key in ("skills", "rules", "agents")}, "overwrite": {"type": "boolean"}}
_SPECS = [
    ("instruction_catalog", "GET", "/catalog", {"workspace": {"type": "string"}}, [], "List canonical skills, project rules and agent definitions available to export."),
    ("instruction_export_preview", "POST", "/preview", _EXPORT, ["target"], "Preview portable host files and warnings for Claude Code, Codex or Cursor; no writes."),
    ("instruction_export", "POST", "/export", _EXPORT, ["target"], "Export canonical instructions and supporting resources. Existing different files are reported as conflicts unless overwrite is explicitly true."),
    ("markdown_hooks_list", "GET", "/hooks", {"workspace": {"type": "string"}}, ["workspace"], "Read project .faustus/hooks/*.md and report malformed files without interrupting the rest."),
    ("markdown_hook_save", "POST", "/hooks/save", {"workspace": {"type": "string"}, "rule": {"type": "object"}, "overwrite": {"type": "boolean"}}, ["workspace", "rule"], "Save a Markdown check for bash/file/stop/prompt. It only warns; it never blocks a tool or requests permission."),
    ("markdown_hooks_propose", "POST", "/hooks/propose", {"session_id": {"type": "string"}, "messages": {"type": "array", "items": {"type": "object"}}}, [], "Analyze a conversation's explicit corrections and propose deterministic mechanical checks with evidence. Never installs proposals automatically; no model required."),
    ("markdown_hooks_evaluate", "POST", "/hooks/evaluate", {"workspace": {"type": "string"}, "event": {"type": "string"}, "context": {"type": "object"}}, ["workspace", "event"], "Evaluate Markdown check patterns/conditions against supplied context; returns advisory matches, never executes or blocks tools."),
]


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=description, inputSchema={"type": "object", "properties": properties, "required": required}) for name, _, _, properties, required, description in _SPECS]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    spec = next((item for item in _SPECS if item[0] == name), None)
    if spec is None:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown tool", "tool": name}))]
    _, method, path, _, _, _ = spec
    url = os.environ.get("FAUSTUS_INSTRUCTIONS_API_URL") or os.environ.get("FAUSTUS_API_URL") or os.environ.get("FAUSTUS_URL") or "http://127.0.0.1:7000"
    headers = {}
    try:
        token = os.environ.get("FAUSTUS_API_TOKEN") or os.environ.get("FAUSTUS_TOKEN", "")
        if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf8").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        async with httpx.AsyncClient() as client:
            response = await client.request(method, url.rstrip("/") + _BASE + path, headers=headers,
                                            **({"params": arguments} if method == "GET" else {"json": arguments}))
            payload = response.json()
            if not response.is_success:
                payload = {"error": payload, "status": response.status_code}
    except (httpx.HTTPError, OSError, ValueError) as error:
        payload = {"error": str(error), "available": False}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def run():
    async with stdio_server() as (reader, writer):
        with stdout_guard():
            await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
