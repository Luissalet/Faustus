"""
goldfish_server.py

MCP server for the elephant-and-goldfish workflows (`src/goldfish.py`): `goldfish_review` --
staged review, sharpening loop, draft-and-review, brainstorm, blind multi-model panel and
document/code drift, each reviewer a fresh model call that never sees the conversation. The
same tool as the chat and the HTTP routes (`/api/goldfish*`).

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is imported.
* **owner-scoped.** Runs live under DATA_DIR/goldfish/<owner>. `ODYSSEUS_MCP_GOLDFISH_OWNER`
  says whose; without it every call is refused.
* A run started here runs in this server's event loop; `wait_s` waits for it.
"""

import asyncio
import json
import os
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

server = Server("goldfish")

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_GOLDFISH_OWNER", "ODYSSEUS_GOLDFISH_OWNER")
_NAMES = ("goldfish_review",)
_OWNER_ERROR = ("Error: the goldfish MCP server has no owner configured, so it cannot tell whose runs these are. "
                "Set ODYSSEUS_MCP_GOLDFISH_OWNER for this server.")


def _owner() -> str:
    for key in _OWNER_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _text(payload) -> list[TextContent]:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return [TextContent(type="text", text=body)]


def _schemas() -> dict:
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    return {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS if s["function"]["name"] in _NAMES}


@server.list_tools()
async def list_tools() -> list[Tool]:
    schemas = _schemas()
    return [Tool(name=n, description=schemas[n]["description"], inputSchema=schemas[n]["parameters"])
            for n in _NAMES if n in schemas]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise."""
    if name not in _NAMES:
        return _text(f"Unknown tool: {name}")
    owner = _owner()
    if not owner:
        return _text(_OWNER_ERROR)
    args = dict(arguments) if isinstance(arguments, dict) else {}
    try:
        from src import goldfish
        data = await goldfish.run_action(owner, args)
    except Exception as exc:  # noqa: BLE001
        return _text(f"Error in {name}: {type(exc).__name__}: {exc}")
    if data.get("error"):
        return _text(f"Error: {data['error']}")
    return _text(goldfish.render(data))


def _preload() -> None:
    try:
        _schemas()
        from src import goldfish  # noqa: F401
    except Exception:  # noqa: BLE001
        pass


async def run():
    _preload()
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
