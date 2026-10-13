"""Native MCP tools for channel operations owned by Faustus."""
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.stdio_guard import guard
from src.constants import internal_api_base

server = Server("channel_connections")
_STRING = {"type": "string"}
_CONNECTION = {"connection_id": _STRING}
_MESSAGE = {**_CONNECTION, "channel_id": _STRING}
_SPECS = {
    "channel_providers": ("GET", "/providers", {}, [], True, "List native Slack, Discord and Matrix providers and their Integration credential presets."),
    "channel_connections": ("GET", "", {}, [], True, "List channel profiles without exposing credentials."),
    "channel_connection_save": ("POST", "", {"provider": {"type": "string", "enum": ["slack", "discord", "matrix"]}, "integration_id": _STRING, "name": _STRING, "connection_id": _STRING}, ["provider", "integration_id"], False, "Create or update a native Slack, Discord or Matrix profile using an existing Integration credential."),
    "channel_spaces": ("POST", "/run", {**_CONNECTION, "parent_id": _STRING, "cursor": _STRING, "limit": {"type": "integer"}}, ["connection_id"], True, "List channels or Discord workspaces; follow next_cursor or use parent_id for guild channels."),
    "channel_history": ("POST", "/run", {**_MESSAGE, "cursor": _STRING, "limit": {"type": "integer"}}, ["connection_id", "channel_id"], True, "Read Slack, Discord or Matrix messages, attachment metadata and the next pagination cursor."),
    "channel_message_send": ("POST", "/run", {**_MESSAGE, "text": _STRING, "transaction_id": _STRING}, ["connection_id", "channel_id", "text"], False, "Send a message to the selected native channel. This sends to an external service."),
    "channel_message_edit": ("POST", "/run", {**_MESSAGE, "text": _STRING, "message_id": _STRING, "transaction_id": _STRING}, ["connection_id", "channel_id", "text", "message_id"], False, "Edit a message; Matrix emits a replacement event. Returns the actual service message/event ID."),
    "channel_reaction_add": ("POST", "/run", {**_MESSAGE, "message_id": _STRING, "reaction": _STRING, "transaction_id": _STRING}, ["connection_id", "channel_id", "message_id", "reaction"], False, "Add a Slack reaction name, Discord emoji or Matrix reaction key."),
}
_OPERATIONS = {"channel_spaces": "spaces", "channel_history": "history", "channel_message_send": "send",
               "channel_message_edit": "edit", "channel_reaction_add": "react"}


@server.list_tools()
async def tools():
    return [Tool(name=name, description=spec[5], inputSchema={"type": "object", "properties": spec[2], "required": spec[3]},
                 annotations=ToolAnnotations(readOnlyHint=spec[4], destructiveHint=False))
            for name, spec in _SPECS.items()]


@server.call_tool()
async def call(name, arguments):
    if name not in _SPECS:
        payload = {"error": "Unknown channel tool", "exit_code": 1}
    else:
        method, path, *_ = _SPECS[name]
        args = dict(arguments or {})
        if name in _OPERATIONS:
            args["operation"] = _OPERATIONS[name]
        try:
            base = os.environ.get("FAUSTUS_CHANNELS_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
            token = os.environ.get("FAUSTUS_TOKEN", "")
            if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
                token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
            headers = {"Authorization": "Bearer " + token} if token else {}
            internal = os.environ.get("FAUSTUS_CHANNELS_INTERNAL_TOKEN", "")
            if internal:
                headers["X-Odysseus-Internal-Token"] = internal
            async with httpx.AsyncClient(timeout=40) as client:
                response = await client.request(method, base.rstrip("/") + "/api/channel-connections" + path,
                                                headers=headers, **({"params": args} if method == "GET" else {"json": args}))
            payload = response.json()
            if not response.is_success:
                payload = {"error": payload, "status_code": response.status_code, "exit_code": 1}
        except (httpx.HTTPError, ValueError, OSError) as error:
            payload = {"error": "Channel API request failed: " + type(error).__name__, "exit_code": 1}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
