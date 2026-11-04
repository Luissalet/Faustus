"""
delivery_server.py

MCP server for the delivery side of a commission: reproducible delivery
packages (Hangar E33, `delivery_package` over `src/delivery_package.py`) and
the scope comparator (E30, `scope_compare` over `src/scope_compare.py`). The
same tools the chat has, so the chat, the HTTP routes
(`/api/delivery-packages*`, `/api/scope-comparisons*`) and this server share
one implementation.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported.
* **owner-scoped.** Packages live under DATA_DIR/delivery_packages/<owner>.
  `ODYSSEUS_MCP_DELIVERY_OWNER` says whose; without it every call is refused.
* **paths**: the configured owner may name any local file except the
  sensitive ones `read_file` refuses (.ssh and the like). Nothing is executed:
  a software root contributes the report `code_graph_handoff` already wrote.
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

server = Server("delivery")

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_DELIVERY_OWNER", "ODYSSEUS_DELIVERY_OWNER")
_NAMES = ("delivery_package", "scope_compare")
_OWNER_ERROR = ("Error: the delivery MCP server has no owner configured, so it cannot tell whose packages these "
                "are. Set ODYSSEUS_MCP_DELIVERY_OWNER for this server.")


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
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "scope_compare":
            from src import scope_compare
            return _text(await asyncio.to_thread(scope_compare.run, owner, args))
        from src import delivery_package
        return _text(await asyncio.to_thread(delivery_package.run, owner, args, confine=False))
    except Exception as exc:  # noqa: BLE001
        return _text(f"Error in {name}: {type(exc).__name__}: {exc}")


def _preload() -> None:
    """Import the heavy modules before the stdio transport starts: on Windows a
    pipe handle serialises synchronous I/O, so an import that touches stdin
    after the reader thread blocks on it would stall ``tools/list``."""
    try:
        _schemas()
        from src import delivery_package, scope_compare  # noqa: F401
    except Exception:  # noqa: BLE001 - the server still starts; the call reports the error
        pass


async def run():
    _preload()
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
