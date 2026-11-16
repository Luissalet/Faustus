"""conversations_server.py

Faustus's conversations as an MCP server, for agents that are not Faustus:
Claude Code, Codex, Cursor or any MCP client launches it over stdio and gets
the Faustus chats and the WhatsApp chats of the paired account
(src/conversation_bridge.py) as tools — list them, read them, wait for new
messages and answer.

    conversations_list   conversation_get   messages_read
    events_poll          events_wait        messages_send

A client config (``FAUSTUS_URL`` is the running Faustus, ``FAUSTUS_TOKEN`` an
API token from Settings; ``GET /api/conversations-mcp/config`` prints it ready
to paste):

    {"mcpServers": {"faustus-conversations": {
        "command": "<python>", "args": ["<repo>/mcp_servers/conversations_server.py"],
        "env": {"FAUSTUS_URL": "http://127.0.0.1:7000", "FAUSTUS_TOKEN": "..."}}}}

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised around the session.
* Reading needs Faustus running; WhatsApp chats also need the WhatsApp bridge paired.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import internal_api_base
from src.conversation_bridge import Bridge, BridgeError
from src.stdio_guard import guard

server = Server("conversations")

_KEY = {"type": "string", "description": "Conversation key from conversations_list: `faustus:<chat id>` or `whatsapp:<jid>`"}
_CURSOR = {"type": "integer", "description": "Epoch milliseconds returned as `cursor` by the previous call. Omit to start from now."}
_SOURCE = {"type": "string", "enum": ["all", "faustus", "whatsapp"], "description": "Which conversations (default all)"}
_ONLY = {"type": "array", "items": {"type": "string"}, "description": "Only these conversation keys (default every conversation)"}

TOOLS = [
    Tool(name="conversations_list",
         description="List Faustus chats and WhatsApp chats, newest activity first, with their key, title, last activity, "
                     "message count (Faustus) or unread count and last text (WhatsApp). A source that cannot be read "
                     "(WhatsApp not paired) is named under `unavailable`.",
         inputSchema={"type": "object", "properties": {
             "source": _SOURCE, "query": {"type": "string", "description": "Filter by title or id"},
             "limit": {"type": "integer", "description": "At most this many (default 30)"}}}),
    Tool(name="conversation_get", description="One conversation's details by key.",
         inputSchema={"type": "object", "properties": {"key": _KEY}, "required": ["key"]}),
    Tool(name="messages_read",
         description="The most recent messages of one conversation, oldest first: role (user/assistant/tool for Faustus, "
                     "me/contact for WhatsApp), sender, text and time.",
         inputSchema={"type": "object", "properties": {
             "key": _KEY, "limit": {"type": "integer", "description": "How many (default 30)"}}, "required": ["key"]}),
    Tool(name="events_poll",
         description="New messages since `cursor` across the conversations, oldest first, and the next `cursor` to pass "
                     "back. Without a cursor it starts from the moment this server started.",
         inputSchema={"type": "object", "properties": {"cursor": _CURSOR, "source": _SOURCE, "conversations": _ONLY}}),
    Tool(name="events_wait",
         description="Like events_poll, but waits until a new message arrives or `timeout_s` passes (default 30, max 300).",
         inputSchema={"type": "object", "properties": {
             "cursor": _CURSOR, "source": _SOURCE, "conversations": _ONLY,
             "timeout_s": {"type": "number", "description": "Seconds to wait (default 30)"}}}),
    Tool(name="messages_send",
         description="Send a message. To a Faustus chat it arrives as a message from this agent and, with `urgent` (default "
                     "true), wakes the chat so Faustus answers it — read the answer with messages_read or events_wait. To a "
                     "WhatsApp chat it is sent from the paired account.",
         inputSchema={"type": "object", "properties": {
             "key": _KEY, "text": {"type": "string"},
             "urgent": {"type": "boolean", "description": "Faustus chats only: wake the chat now (default true)"},
             "sender": {"type": "string", "description": "Faustus chats only: how this agent is named in the chat (default `mcp`)"}},
             "required": ["key", "text"]}),
]


def _token() -> str:
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        try:
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    return token


def _headers() -> dict:
    headers = {}
    if _token():
        headers["Authorization"] = "Bearer " + _token()
    if os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN"):
        headers["X-Odysseus-Internal-Token"] = os.environ["FAUSTUS_INTERNAL_TOOL_TOKEN"]
    return headers


def _base() -> str:
    return (os.environ.get("FAUSTUS_URL") or os.environ.get("FAUSTUS_INTERNAL_API_URL") or internal_api_base()).rstrip("/")


def _request(method, path, params=None, json=None):
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            r = client.request(method, _base() + path, params=params, json=json, headers=_headers())
    except httpx.HTTPError as exc:
        raise BridgeError(f"Faustus did not answer at {_base()} ({type(exc).__name__})") from exc
    try:
        payload = r.json()
    except ValueError:
        payload = r.text[:300]
    return r.status_code, payload


_bridge = Bridge(_request)


def _dispatch(name: str, a: dict):
    if name == "conversations_list":
        return _bridge.conversations(a.get("source") or "all", a.get("query") or "", a.get("limit") or 30)
    if name == "conversation_get":
        return _bridge.conversation(a.get("key") or "")
    if name == "messages_read":
        return _bridge.read(a.get("key") or "", a.get("limit") or 30)
    if name == "events_poll":
        return _bridge.poll(a.get("cursor"), a.get("source") or "all", a.get("conversations"))
    if name == "events_wait":
        return _bridge.wait(a.get("cursor"), a.get("timeout_s") or 30, a.get("source") or "all", a.get("conversations"))
    if name == "messages_send":
        return _bridge.send(a.get("key") or "", a.get("text") or "",
                            urgent=a.get("urgent") is not False, sender=a.get("sender") or "mcp")
    raise BridgeError(f"unknown tool {name}")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    try:
        payload, error = await asyncio.to_thread(_dispatch, name, dict(arguments or {})), False
    except (BridgeError, ValueError, TypeError) as exc:
        payload, error = {"error": str(exc)}, True
    return CallToolResult(isError=error, content=[TextContent(
        type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
