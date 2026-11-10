"""unified_search_server.py

MCP server for "find that thing about X, wherever it is" (src/unified_search.py, the same search as
``GET /api/search/all`` and the Ctrl+K palette): one query fanned out to chats, brain notes, quick notes, documents,
gallery images, skills, board issues, projects and the agent's settings, each source under a time budget, mixed into
one ranked list where every hit carries the Studio address that opens it.

Same discipline as the other built-in servers: stdout is the JSON-RPC stream (``src/stdio_guard.py`` is raised before
anything prints), the work module is imported on first use, and ``call_tool`` never raises. Read-only. Owner-scoped
through ``ODYSSEUS_MCP_SEARCH_OWNER`` (or the ``owner`` argument); without either it searches the whole install.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("unified_search")

_OWNER_ENV_KEYS = ("ODYSSEUS_MCP_SEARCH_OWNER",)
_SOURCES = ["chats", "brain", "notes", "documents", "gallery", "skills", "board", "projects", "settings"]

TOOLS = [
    Tool(name="search_everything",
         description=("Search everything the owner has at once -- past chats word for word, brain notes, quick notes, "
                      "documents, gallery images, skills, board issues, projects and agent settings -- and get one "
                      "ranked list; each hit has type, title, snippet, date and the Studio address that opens it. "
                      "Narrow with `types`; `after`/`before` (ISO dates) bound chat messages ('as of' a date). "
                      "Busca en todo a la vez. Read-only."),
         inputSchema={"type": "object", "required": ["q"], "properties": {
             "q": {"type": "string", "description": "Words to find; every word must appear."},
             "types": {"type": "array", "items": {"type": "string", "enum": _SOURCES},
                       "description": "Only these sources; empty or absent = all."},
             "limit": {"type": "integer", "minimum": 1, "maximum": 25, "description": "Hits per source; default 8."},
             "after": {"type": "string", "description": "Chats: messages from this ISO date on."},
             "before": {"type": "string", "description": "Chats: only what had been said by this ISO date."},
             "owner": {"type": "string", "description": "Whose things; default the server's configured owner."}}}),
    Tool(name="search_sources",
         description=("The sources search_everything can look in, with what each covers and the screen a hit opens. "
                      "Fuentes de la búsqueda en todo. Read-only."),
         inputSchema={"type": "object", "properties": {}}),
]

_SOURCE_HELP = {
    "chats": "past conversations word for word (full text), opens the chat at the message",
    "brain": "brain vault notes, opens /brain at the note",
    "notes": "quick notes (title, body, checklist items), opens /notes",
    "documents": "documents (title and current text), opens the document",
    "gallery": "gallery images (prompt, tags, AI tags), opens the library",
    "skills": "skills by relevance, opens /skills",
    "board": "board issues of the first projects, opens the issue",
    "projects": "projects by name, folder, instructions or workspace path, opens the project",
    "settings": "agent, browser and desktop options (key, label, help), opens Settings > Agent filtered to the key",
}


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _owner(a: dict) -> str | None:
    given = str(a.get("owner") or "").strip()
    if given:
        return given
    for key in _OWNER_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return None


def _dispatch(name: str, a: dict):
    if name == "search_sources":
        return {"sources": [{"type": s, "covers": _SOURCE_HELP[s]} for s in _SOURCES]}
    from src import unified_search

    types = a.get("types")
    wanted = [str(t) for t in types] if isinstance(types, list) and types else None
    limit = max(1, min(25, int(a.get("limit") or 8)))
    return unified_search.search(_owner(a), str(a.get("q") or ""), wanted, limit,
                                 after=str(a.get("after") or ""), before=str(a.get("before") or ""))


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(TOOLS)


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name not in {t.name for t in TOOLS}:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        result = await asyncio.to_thread(_dispatch, name, args)
    except Exception as exc:  # noqa: BLE001 - a bad argument comes back as text
        return _text_result(f"Error in {name}: {exc}")
    return _text_result(json.dumps(result, indent=2, ensure_ascii=False, default=str))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
