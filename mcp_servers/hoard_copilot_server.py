"""MCP facade over the running Faustus copilot service, not another harness."""
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
from src.stdio_guard import guard

server = Server("hoard-copilot")
CONTEXT = {"type": "object", "description": "Hoard origin with app_id/data_space_id, native project, active objects and selection."}
OPERATIONS = {
    "hoard_context_resolve": ("POST", "/project-links/resolve", "Resolve the Hoard project into the same stable Faustus working project.", {"context": CONTEXT}),
    "hoard_conversation_list": ("GET", "/conversations", "List canonical Faustus chats by Hoard, data space or native project.", {key: {"type": "string"} for key in ("app_id", "data_space_id", "external_project_id")}),
    "hoard_conversation_open": ("POST", "/conversations", "Create a canonical Faustus chat associated with the Hoard context; does not duplicate domain data.", {"context": CONTEXT, "name": {"type": "string"}}),
    "hoard_conversation_get": ("GET", "/conversations/{session_id}", "Read the same chat, history and working references visible in Faustus and the Hoard.", {"session_id": {"type": "string"}}),
    "hoard_conversation_import": ("POST", "/imports", "Import a legacy Hoard conversation idempotently, preserving its source IDs and metadata without executing historical instructions.", {"context": CONTEXT, "source_thread_id": {"type": "string"}, "name": {"type": "string"}, "messages": {"type": "array", "items": {"type": "object"}}}),
    "hoard_turn_start": ("POST", "/api/chat_stream", "Start the normal Faustus agent turn with Hoard context; closing the observer leaves the run executing on the server.", {"session_id": {"type": "string"}, "message": {"type": "string"}, "context": CONTEXT, "client_message_id": {"type": "string"}}),
    "hoard_turn_status": ("GET", "/api/chat/stream_status/{session_id}", "Read the actual state of the shared Faustus execution without starting or repeating it.", {"session_id": {"type": "string"}}),
    "hoard_turn_stop": ("POST", "/api/chat/stop/{session_id}", "Stop the shared Faustus turn; domain changes already applied remain in their Hoard.", {"session_id": {"type": "string"}, "scope": {"type": "string", "enum": ["generation", "task", "work"]}}),
}


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=value[2], inputSchema={"type": "object", "properties": value[3]}) for name, value in OPERATIONS.items()]


@server.call_tool()
async def call_tool(name, arguments):
    if name not in OPERATIONS:
        raise ValueError("Unknown copilot operation")
    method, path, _, _ = OPERATIONS[name]
    payload = dict(arguments or {})
    if "{session_id}" in path:
        from urllib.parse import quote
        path = path.replace("{session_id}", quote(str(payload.pop("session_id")), safe=""))
    token = os.environ.get("FAUSTUS_TOKEN", "")
    token_file = os.environ.get("FAUSTUS_TOKEN_FILE", "")
    if token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    base = os.environ.get("FAUSTUS_URL", "http://127.0.0.1:7000").rstrip("/")
    url = base + (path if path.startswith("/api/") else "/api/hoard-copilot" + path)
    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
        if name == "hoard_turn_start":
            payload["session"] = payload.pop("session_id")
            payload["hoard_context"] = payload.pop("context", None)
            payload["mode"] = "agent"
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                result = {"session_id": payload["session"], "run_id": response.headers.get("X-Odysseus-Run-Id"),
                          "replayed": response.headers.get("X-Faustus-Idempotent-Replay") == "1"}
                if not result["run_id"]:
                    raise ValueError("The turn did not return an execution identity")
            return [TextContent(type="text", text=json.dumps(result))]
        response = await client.request(method, url, headers=headers,
                                        params=payload if method == "GET" else None,
                                        json=payload if method != "GET" else None)
        response.raise_for_status()
    return [TextContent(type="text", text=json.dumps(response.json(), ensure_ascii=False))]


async def main():
    # Warm the settings used by the guard before Windows starts stdio readers.
    # Importing them for the first time inside a live stdio session can hang.
    with guard():
        from src import settings  # noqa: F401
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
