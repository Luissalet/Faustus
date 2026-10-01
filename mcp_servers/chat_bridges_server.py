"""chat_bridges_server.py

MCP server for the chat bridges (src/chat_bridges/). One read-only tool,
`telegram_status`: whether the Telegram bridge is enabled and running, the bot
it found, its last error, the chats allowed to talk to the agent, the
conversations they map to and the chats that were refused.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported, like the other built-in servers.
* This process is not the app, so it cannot ask the live poller. The poller
  leaves a snapshot of itself in the bridge's sqlite file; this reads that, the
  settings and the chat mapping (src/chat_bridges/status.py). A snapshot older
  than ~90 s reads as "not running".
* Nothing is written, nothing is sent to Telegram, and the bot token is never
  part of the answer (only whether one is saved).
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("chat_bridges")


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="telegram_status",
            description=(
                "Report the state of the Telegram chat bridge, which lets people talk to this "
                "Faustus agent from a Telegram chat. Returns whether it is enabled (setting) and "
                "running (a recent heartbeat from the poller), whether a bot token is saved (never "
                "the token), the bot's username, the last error and the reason it stopped if it did "
                "(for example a rejected token), the mode, the allowed chat ids, each chat's mapped "
                "conversation with its Studio link, and the chats that were refused with their ids "
                "so they can be added to the allow-list. Estado del puente de Telegram. Read-only."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name != "telegram_status":
        return _text_result(f"Unknown tool: {name}")
    try:
        from src.chat_bridges.status import read_status

        status = await asyncio.to_thread(read_status)
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"Error in telegram_status: {type(exc).__name__}: {exc}")
    return _text_result(json.dumps({"ok": True, **status}, indent=2, ensure_ascii=False, default=str))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
