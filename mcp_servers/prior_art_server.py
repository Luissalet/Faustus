"""prior_art_server.py

MCP server exposing prior-art verification: reuse, adapt, or write, with
every repository name checked live against the GitHub API before it reaches
the user (Reach models recall repo names badly — they invent plausible-
looking ones or cite dead projects).

Same first constraint as `brain_server.py` / `context_engine_server.py`, for
the same reason:

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported.

Unlike brain/context/memory, this server has no owner-scoped store to
protect — `src.prior_art` reads/writes only its own SQLite cache and report
log (never a workspace, never per-owner data), and its only outbound calls
are read-only GETs to `api.github.com`. So there is no owner env var and no
write refusal here: every tool is safe to run with no configuration at all.
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

server = Server("prior_art")

_engine: dict = {}
_initialized = False


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload) -> list[TextContent]:
    import json

    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _ensure_init() -> None:
    """Import `src.prior_art` on first use, not at server start — a server
    only ever asked to list its tools should not pay for it."""
    global _initialized
    if _initialized:
        return
    _initialized = True
    from src import prior_art

    _engine["prior_art"] = prior_art


def _limit(value, default: int, ceiling: int) -> int:
    try:
        wanted = int(value)
    except (TypeError, ValueError):
        wanted = default
    return max(1, min(wanted, ceiling))


# ── the tool surface ───────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="prior_art_rubric",
            description=(
                "Before building something, get the decomposition checklist: split "
                "the idea into 3-10 components, pick reuse/adapt/write for each, and "
                "the JSON slate shape to fill in for prior_art_verify. Antes de "
                "construir esto, qué hay ya / ya existe una librería para esto. "
                "No network call. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "idea": {"type": "string", "description": "The idea/feature to decompose."},
                    "stack": {"type": "string", "description": "Target language/stack, e.g. 'python'."},
                    "license": {"type": "string", "description": "Target project's license, e.g. 'MIT'."},
                    "constraints": {"type": "string", "description": "Any constraint the decomposition must respect."},
                },
                "required": ["idea"],
            },
        ),
        Tool(
            name="prior_art_verify",
            description=(
                "Verify a filled-in slate live against the GitHub API: existence, "
                "archived/fork status, health (last push), stars, and license "
                "compatibility for every reuse/adapt candidate. Verifica si estos "
                "repos existen y son de fiar. Returns a ranked table, a "
                "confirmed/downgrade/replace outcome per component, next actions, "
                "and a saved report id. Read-only (network)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "slate": {
                        "type": "object",
                        "description": "{idea?, components: [{name, verdict, repos: [owner/name,...], rationale}]}",
                        "properties": {
                            "idea": {"type": "string"},
                            "components": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "verdict": {"type": "string", "enum": ["reuse", "adapt", "write"]},
                                        "repos": {"type": "array", "items": {"type": "string"}},
                                        "rationale": {"type": "string"},
                                    },
                                    "required": ["name", "verdict"],
                                },
                            },
                        },
                    },
                    "target_license": {"type": "string", "description": "Target project's license, checked against each 'reuse' candidate."},
                    "stack": {"type": "string"},
                },
                "required": ["slate"],
            },
        ),
        Tool(
            name="prior_art_search",
            description=(
                "Search GitHub repositories when there is no candidate to verify "
                "yet -- results carry the same verified fields as prior_art_verify "
                "(license, stars, last push, archived/fork). Busca en GitHub "
                "alternativas de código abierto para esto. Read-only (network)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text GitHub repository search query."},
                    "language": {"type": "string", "description": "Restrict to a GitHub-recognized language name."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                    "include_stale": {"type": "boolean", "description": "Include repos with no push in 2+ years (default false)."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="prior_art_report",
            description=(
                "Read back one saved prior_art_verify report by id (e.g. "
                "'PA-000123'), or list recent ones when no id is given. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "A saved report id. Omit to list recent reports instead."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
            },
        ),
    ]


# ── the handlers ───────────────────────────────────────────────────────────

def _tool_rubric(args: dict) -> list[TextContent]:
    idea = str(args.get("idea") or "").strip()
    if not idea:
        return _text_result("prior_art_rubric: `idea` is required")
    result = _engine["prior_art"].rubric(
        idea, stack=str(args.get("stack") or ""), license=str(args.get("license") or ""),
        constraints=str(args.get("constraints") or ""),
    )
    return _json_result({"ok": True, **result})


def _tool_verify(args: dict) -> list[TextContent]:
    slate = args.get("slate")
    if slate is None:
        return _text_result("prior_art_verify: `slate` is required")
    result = _engine["prior_art"].verify(
        slate, target_license=str(args.get("target_license") or ""),
        stack=str(args.get("stack") or ""),
    )
    return _json_result({"ok": True, **result})


def _tool_search(args: dict) -> list[TextContent]:
    query = str(args.get("query") or "").strip()
    if not query:
        return _text_result("prior_art_search: `query` is required")
    result = _engine["prior_art"].search(
        query, language=str(args.get("language") or ""),
        limit=_limit(args.get("limit"), 8, 25),
        include_stale=bool(args.get("include_stale")),
    )
    return _json_result({"ok": True, **result})


def _tool_report(args: dict) -> list[TextContent]:
    prior_art = _engine["prior_art"]
    report_id = str(args.get("id") or "").strip()
    if report_id:
        found = prior_art.report(report_id)
        if found is None:
            return _text_result(f"prior_art_report: no such report {report_id!r}")
        return _json_result({"ok": True, "report": found})
    rows = prior_art.reports(_limit(args.get("limit"), 20, 200))
    return _json_result({"ok": True, "reports": rows})


_HANDLERS = {
    "prior_art_rubric": _tool_rubric,
    "prior_art_verify": _tool_verify,
    "prior_art_search": _tool_search,
    "prior_art_report": _tool_report,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise — an exception here would kill every other
    tool on this server, not just the one call that failed."""
    if name not in _HANDLERS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}

    try:
        _ensure_init()
    except Exception as exc:  # noqa: BLE001 - an unimportable engine is a message
        return _text_result(f"Error: prior_art could not be loaded: {exc}")

    try:
        return _HANDLERS[name](args)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
