"""
observations_server.py

MCP server for observations: the notes the small model wrote about earlier tool
calls (what changed, what was learned, which files). Retrieval is progressive,
so a client spends tokens only on what it chose to open:

    obs_search    -> an index: id, date, type, title
    obs_timeline  -> what happened just before and after chosen notes
    obs_details   -> the full records, at most five
    obs_recent    -> the newest notes, titles only

Same constraints as the other built-in servers:

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised first.
* **read-only.** Nothing here writes; the notes are written by the background
  worker inside the app (`src/observations`). Every tool carries readOnlyHint.
* **scoped by owner.** `ODYSSEUS_MCP_OBSERVATIONS_OWNER` says whose notes this
  server may read; without it reads degrade to install-wide (a single-user
  install's normal state).
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("observations")

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_OBSERVATIONS_OWNER", "ODYSSEUS_OBSERVATIONS_OWNER")
_engine: dict = {}
_initialized = False

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

TOOLS = [
    Tool(
        name="obs_search",
        description=(
            "Search notes on past work: index of id, date, type, title. Busca trabajo previo / qué se hizo. Read-only.\n"
            "Step 1 of 3: search, then obs_timeline for context, then obs_details (max 5 ids) for the full record."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to look for; empty = the most recent."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Default 20."},
                "type": {"type": "string",
                         "enum": ["bugfix", "feature", "refactor", "change", "discovery", "decision", "other"],
                         "description": "Only this kind of note."},
                "project": {"type": "string", "description": "Workspace path or project id to restrict to."},
            },
        },
        annotations=_READ_ONLY,
    ),
    Tool(
        name="obs_timeline",
        description=(
            "Notes just before and after chosen ones. Cronología de notas / qué pasó antes y después. Read-only.\n"
            "Step 2 of 3. Anchors are ids from obs_search; S<n> anchors a turn summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "anchor_ids": {"type": "array", "items": {"type": ["string", "integer"]}, "minItems": 1, "maxItems": 5,
                               "description": "Observation ids (12) or summary ids (S3)."},
                "before": {"type": "integer", "minimum": 0, "maximum": 10, "description": "Earlier notes per anchor; default 2."},
                "after": {"type": "integer", "minimum": 0, "maximum": 10, "description": "Later notes per anchor; default 2."},
            },
            "required": ["anchor_ids"],
        },
        annotations=_READ_ONLY,
    ),
    Tool(
        name="obs_details",
        description=(
            "Full records of up to 5 notes by id. Detalle completo de notas / abrir una observación por id. Read-only.\n"
            "Step 3 of 3: facts, narrative, concepts, files read and modified. Ask only for the ids you need."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": ["string", "integer"]}, "minItems": 1, "maxItems": 5,
                        "description": "Observation ids (12) or summary ids (S3); at most 5."},
            },
            "required": ["ids"],
        },
        annotations=_READ_ONLY,
    ),
    Tool(
        name="obs_recent",
        description=(
            "Newest notes on past work, titles only. Notas recientes / qué se ha hecho últimamente. Read-only.\n"
            "Use obs_details to open one; use obs_search to look for a topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Default 20."},
                "type": {"type": "string",
                         "enum": ["bugfix", "feature", "refactor", "change", "discovery", "decision", "other"]},
                "project": {"type": "string", "description": "Workspace path or project id to restrict to."},
            },
        },
        annotations=_READ_ONLY,
    ),
]


def _configured_owner() -> str:
    for key in _OWNER_ENV_KEYS:
        owner = os.environ.get(key, "").strip()
        if owner:
            return owner
    return ""


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload) -> list[TextContent]:
    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _ensure_init() -> None:
    """Import the observations package on first use, not at server start - a
    server that is only ever asked to list its tools should not pay for it."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    from src import observations

    _engine["observations"] = observations


def _limit(value, default: int, ceiling: int) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted, ceiling))


def _ids(value, ceiling: int) -> list:
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, (str, int)) and not isinstance(v, bool)][:ceiling]


def _tool_search(owner: str, args: dict) -> list[TextContent]:
    obs = _engine["observations"]
    results = obs.search(str(args.get("query") or ""), _limit(args.get("limit"), 20, 20), owner=owner,
                         project=str(args.get("project") or ""), kind=str(args.get("type") or "").strip().lower())
    return _json_result({"results": results, "next": "obs_timeline (context) or obs_details (full record, max 5 ids)"})


def _tool_timeline(owner: str, args: dict) -> list[TextContent]:
    obs = _engine["observations"]
    anchors = _ids(args.get("anchor_ids"), 5)
    if not anchors:
        raise ValueError("anchor_ids needs at least one id from obs_search")
    before = max(0, min(int(args.get("before", 2) or 0), 10))
    after = max(0, min(int(args.get("after", 2) or 0), 10))
    return _json_result({"entries": obs.timeline(anchors, before, after, owner=owner)})


def _tool_details(owner: str, args: dict) -> list[TextContent]:
    obs = _engine["observations"]
    ids = _ids(args.get("ids"), 50)
    if not ids:
        raise ValueError("ids needs at least one id from obs_search or obs_timeline")
    if len(ids) > 5:
        raise ValueError("at most 5 ids per call; narrow down with obs_search or obs_timeline first")
    return _json_result({"records": obs.details(ids, owner=owner)})


def _tool_recent(owner: str, args: dict) -> list[TextContent]:
    obs = _engine["observations"]
    results = obs.recent(_limit(args.get("limit"), 20, 100), owner=owner, project=str(args.get("project") or ""),
                         kind=str(args.get("type") or "").strip().lower())
    return _json_result({"results": results})


_HANDLERS = {
    "obs_search": _tool_search,
    "obs_timeline": _tool_timeline,
    "obs_details": _tool_details,
    "obs_recent": _tool_recent,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(TOOLS)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise - an exception here would kill every other
    tool on this server, not just the one call that failed."""
    if name not in _HANDLERS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        _ensure_init()
    except Exception as exc:  # noqa: BLE001 - an unimportable store is a message
        return _text_result(f"Error: observations could not be loaded: {exc}")
    try:
        return _HANDLERS[name](_configured_owner(), args)
    except ValueError as exc:
        return _text_result(f"{name}: {exc}")
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream,
                             server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
