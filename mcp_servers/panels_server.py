"""
panels_server.py

MCP server for the user's panels (`src/panels.py`): the same eight `panel_*` tools the chat has
(create, update, get, list, delete, check, versions, restore), so the chat, the HTTP routes
(`/api/panels*`) and this server share one implementation.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is imported.
* panels are stored under DATA_DIR/panels (one user's Faustus); `panel_check` opens the page on the
  running Faustus (`ODYSSEUS_INTERNAL_BASE`, default http://127.0.0.1:7000) in headless Chromium.
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

server = Server("panels")

_NAMES = ("panel_create", "panel_update", "panel_get", "panel_list", "panel_delete", "panel_check",
          "panel_versions", "panel_restore")


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
    args = arguments if isinstance(arguments, dict) else {}
    try:
        from src.agent_tools.panel_tools import HANDLERS
        out = await HANDLERS[name]().execute(args, {})
    except Exception as exc:  # noqa: BLE001
        return _text(f"Error in {name}: {type(exc).__name__}: {exc}")
    if out.get("error"):
        return _text(f"Error: {out['error']}")
    return _text(out.get("result") if out.get("result") is not None else out.get("output", ""))


def _preload() -> None:
    try:
        _schemas()
        from src import panels  # noqa: F401
    except Exception:  # noqa: BLE001
        pass


async def run():
    _preload()
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
