"""
code_graph_server.py

MCP server exposing the code graph: symbol search, call/import tracing,
change impact, architecture, communities (what modules this repo is made
of), execution flows (which paths run through it and how critical they
are), a symbol's source snippet, and a deterministic change-risk score.

Same two constraints as `brain_server.py`/`context_engine_server.py`, for
the same reasons:

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported.
* **workspace confinement is the tool layer's, not this server's.** Every
  handler here is a thin wrapper over `src.code_graph`'s own public
  functions, which already confine `root`/`workspace` through
  `src.tool_execution._resolve_search_root` (the same guard `read_file` and
  grep/glob use). Unlike the brain/memory servers this one is not
  owner-scoped: a code graph has no owner, only a workspace, and every tool
  here takes one explicitly (defaulting to the active/primary root exactly
  as `code_graph_search` et al. already do when called with no `root`).
"""

import asyncio
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

server = Server("code_graph")

_engine: dict = {}
_initialized = False


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload) -> list[TextContent]:
    import json

    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _ensure_init() -> None:
    """Import the code graph on first use, not at server start -- a server
    only ever asked to list its tools should not pay for it."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    from src import code_graph

    _engine["code_graph"] = code_graph


def _limit(value, default: int, ceiling: int) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted, ceiling))


_ROOT_PROP = {"root": {"type": "string",
                       "description": "Workspace root. Omit for the active workspace."}}


# ── the tool surface ───────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="code_graph_search",
            description=(
                "Search the code graph for a symbol by name/pattern without reading files. "
                "Busca un símbolo en el grafo de código / dónde está definido X. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Name or partial name to search for."},
                    "kinds": {"type": "array", "items": {"type": "string"},
                             "description": "Restrict to these kinds (module, class, function, method, constant, route, tool)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    **_ROOT_PROP,
                },
                "required": ["pattern"],
            },
        ),
        Tool(
            name="code_graph_trace",
            description=(
                "Trace a call/import path between two named symbols (BFS, certainty per hop). "
                "Traza el camino de llamadas entre dos símbolos / how does A eventually reach B. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_symbol": {"type": "string", "description": "Starting symbol name/qualname."},
                    "to_symbol": {"type": "string", "description": "Target symbol name/qualname."},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 8},
                    **_ROOT_PROP,
                },
                "required": ["from_symbol", "to_symbol"],
            },
        ),
        Tool(
            name="code_graph_impact",
            description=(
                "What else can break and which tests to run, from a symbol or the current git diff. "
                "Qué se rompe si toco esta función / qué tests debería correr. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol to seed from; omit to seed from the current git diff."},
                    "base_ref": {"type": "string", "description": "Git ref to diff against (default HEAD)."},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 6},
                    **_ROOT_PROP,
                },
                "required": [],
            },
        ),
        Tool(
            name="code_graph_architecture",
            description=(
                "One-call architecture summary: languages, routes, fan-in/out, hotspots. "
                "Dame la arquitectura de este repo / lay of the land. Read-only."
            ),
            inputSchema={"type": "object", "properties": {**_ROOT_PROP}},
        ),
        Tool(
            name="code_graph_communities",
            description=(
                "What parts this repo is made of: deterministic module clustering (level 0/1). "
                "De qué partes se compone el repo / give me the module map. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "level": {"type": "integer", "minimum": 0, "maximum": 1},
                    "id": {"type": "string", "description": "A community id, name, or symbol -- one community's detail."},
                    "refresh": {"type": "boolean"},
                    "summarize": {"type": "boolean"},
                    **_ROOT_PROP,
                },
                "required": [],
            },
        ),
        Tool(
            name="code_graph_flows",
            description=(
                "Execution flows from every real entry point, ranked by criticality, or one's call tree. "
                "Qué flujos de ejecución hay / show me the call tree from this route. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry": {"type": "string", "description": "An entry point name/qualname -- that flow's call tree."},
                    "id": {"type": "string", "description": "A flow id -- that flow's call tree."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    "sort": {"type": "string", "enum": ["criticality", "size"]},
                    "refresh": {"type": "boolean"},
                    **_ROOT_PROP,
                },
                "required": [],
            },
        ),
        Tool(
            name="code_graph_affected_flows",
            description=(
                "Which execution flows a symbol, or the current git diff, passes through. "
                "Qué flujos toca este cambio / which flows does this symbol sit inside. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name/qualname; omit to use the current git diff."},
                    "base_ref": {"type": "string", "description": "Git ref to diff against when symbol is omitted (default HEAD)."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                    **_ROOT_PROP,
                },
                "required": [],
            },
        ),
        Tool(
            name="code_graph_snippet",
            description=(
                "Exactly one symbol's source lines, nothing else from the file. "
                "Dame solo el código de esa función / show me just that definition. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {"symbol": {"type": "string"}, **_ROOT_PROP},
                "required": ["symbol"],
            },
        ),
        Tool(
            name="code_graph_change_risk",
            description=(
                "Deterministic 0-100 change-risk score for paths/symbols or the current diff. "
                "Qué tan arriesgado es este cambio / is this file safe to edit. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"},
                             "description": "File paths/symbol names; omit to score the current git diff."},
                    "base_ref": {"type": "string"},
                    **_ROOT_PROP,
                },
                "required": [],
            },
        ),
    ]


# ── the handlers ───────────────────────────────────────────────────────────

def _tool_search(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    out = cg.search_graph(str(args.get("pattern") or ""),
                          kinds=tuple(args.get("kinds") or ()),
                          limit=_limit(args.get("limit"), 40, 200),
                          workspace=str(args.get("root") or ""))
    return _json_result(out)


def _tool_trace(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    out = cg.trace_path(str(args.get("from_symbol") or ""), str(args.get("to_symbol") or ""),
                        workspace=str(args.get("root") or ""),
                        max_depth=_limit(args.get("max_depth"), 5, 8))
    return _json_result(out)


def _tool_impact(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    out = cg.impact(str(args.get("symbol") or ""), workspace=str(args.get("root") or ""),
                    base_ref=str(args.get("base_ref") or "HEAD"),
                    depth=_limit(args.get("depth"), 3, 6))
    return _json_result(out)


def _tool_architecture(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    return _json_result(cg.get_architecture(str(args.get("root") or "")))


def _tool_communities(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    target = str(args.get("id") or "").strip()
    root = str(args.get("root") or "")
    if target:
        return _json_result(cg.community(root, target))
    out = cg.communities(root, level=_limit(args.get("level"), 0, 1),
                        refresh=bool(args.get("refresh")),
                        summarize=bool(args.get("summarize")))
    return _json_result(out)


def _tool_flows(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    root = str(args.get("root") or "")
    flow_id = str(args.get("id") or "").strip()
    entry = str(args.get("entry") or "").strip()
    if flow_id or entry:
        return _json_result(cg.flow(root, flow_id or entry))
    out = cg.flows(root, limit=_limit(args.get("limit"), 20, 500),
                   sort=str(args.get("sort") or "criticality"),
                   refresh=bool(args.get("refresh")))
    return _json_result(out)


def _tool_affected_flows(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    out = cg.affected_flows(str(args.get("symbol") or ""), workspace=str(args.get("root") or ""),
                            base_ref=str(args.get("base_ref") or "HEAD"),
                            limit=_limit(args.get("limit"), 20, 500))
    return _json_result(out)


def _tool_snippet(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    return _json_result(cg.snippet(str(args.get("symbol") or ""),
                                   workspace=str(args.get("root") or "")))


def _tool_change_risk(args: dict) -> list[TextContent]:
    cg = _engine["code_graph"]
    paths = args.get("paths")
    if not isinstance(paths, list):
        paths = None
    out = cg.change_risk(paths, workspace=str(args.get("root") or ""),
                         base_ref=str(args.get("base_ref") or "HEAD"))
    return _json_result(out)


_HANDLERS = {
    "code_graph_search": _tool_search,
    "code_graph_trace": _tool_trace,
    "code_graph_impact": _tool_impact,
    "code_graph_architecture": _tool_architecture,
    "code_graph_communities": _tool_communities,
    "code_graph_flows": _tool_flows,
    "code_graph_affected_flows": _tool_affected_flows,
    "code_graph_snippet": _tool_snippet,
    "code_graph_change_risk": _tool_change_risk,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill every other
    tool on this server, not just the one call that failed."""
    if name not in _HANDLERS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}

    try:
        _ensure_init()
    except Exception as exc:  # noqa: BLE001 - an unimportable engine is a message
        return _text_result(f"Error: the code graph could not be loaded: {exc}")

    try:
        return _HANDLERS[name](args)
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
