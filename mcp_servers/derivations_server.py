"""
derivations_server.py

MCP server for recipes learned from finished work, derived results with
selective rebuild, and the decision/effect map. The same three tools the chat
has (`recipe_learn`, `derivation_graph`, `decision_map`), over
`src/derivation_ops.py`, so there is one implementation behind the chat, the
HTTP routes (`/api/recipes/learn*`, `/api/derivations/*`) and this server.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported.
* **owner-scoped.** Recipes, graphs and maps are per owner (DATA_DIR/recipes/<owner>,
  DATA_DIR/derivations/<owner>). `ODYSSEUS_MCP_DERIVATIONS_OWNER` says whose;
  without it every call is refused rather than written into nobody's store.
* **paths are confined** to the active workspace like `read_file`'s.
* **nothing runs unless asked.** `derivation_graph rebuild` hands back the next
  stale node for the caller to run with its own approved tools. Only
  `recipe_learn action=execute` (or `try` with `mode=execute`) runs a recipe's
  steps, through the normal tool dispatcher as the configured owner, in the
  `workspace` given, with the same policy gates as any other call.
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

server = Server("derivations")

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_DERIVATIONS_OWNER", "ODYSSEUS_DERIVATIONS_OWNER")
_NAMES = ("recipe_learn", "derivation_graph", "decision_map")
_OWNER_ERROR = ("Error: the derivations MCP server has no owner configured, so it cannot tell whose recipes and "
                "graphs these are. Set ODYSSEUS_MCP_DERIVATIONS_OWNER for this server.")


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
        from src import derivation_ops
        if name == "recipe_learn":
            return _text(await derivation_ops.run_recipe_async(owner, args))
        fn = {"derivation_graph": derivation_ops.run_graph, "decision_map": derivation_ops.run_map}[name]
        return _text(await asyncio.to_thread(fn, owner, args))
    except Exception as exc:  # noqa: BLE001
        return _text(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
