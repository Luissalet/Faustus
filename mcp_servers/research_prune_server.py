"""research_prune_server.py

MCP server exposing `page_prune`: cut a web page (a URL, raw HTML or extracted
text) down to the blocks that look like content AND match a question, with
their scores (src/research_prune.py). A local assistant can use it to read a
long page cheaply or to see why a page yields little.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
  anything else is imported, like the other built-in servers.
* No owner-scoped store and no writes: given a URL it makes one guarded
  read-only GET (the same fetcher `web_fetch` uses); given HTML or text it only
  computes. So there is no owner environment variable.
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

server = Server("research_prune")


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="page_prune",
            description=(
                "Cut a web page down to what answers a question, without a model. Give a "
                "url (fetched read-only), or raw html, or already extracted text, plus the "
                "query the page is read for. Blocks are scored by text density, link "
                "density, tag and class/id hints; those above the threshold are ranked with "
                "BM25 (accents folded, Spanish and English) and the best are returned in "
                "document order under a character cap, with the page title and best match "
                "always kept. Returns the pruned text, original vs pruned characters, blocks "
                "kept, the top BM25 score and each block's score. Recorta una página a lo "
                "relevante para una pregunta. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The question or topic the page is read for."},
                    "url": {"type": "string", "description": "Page to fetch and prune (http/https)."},
                    "html": {"type": "string", "description": "Raw HTML to prune instead of a url."},
                    "text": {"type": "string", "description": "Extracted Markdown/plain text to prune instead of a url."},
                    "title": {"type": "string", "description": "Page title to keep."},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 60000,
                                  "description": "Character cap of the pruned text (default 6000)."},
                    "threshold": {"type": "number", "minimum": 0, "maximum": 1,
                                  "description": "Minimum block score (default 0.48)."},
                    "include_blocks": {"type": "boolean", "description": "List every block's score (default true)."},
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name != "page_prune":
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        from src.agent_tools.prune_tools import PagePruneTool

        result = await PagePruneTool().execute(json.dumps(args), {})
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"Error in page_prune: {type(exc).__name__}: {exc}")
    if result.get("exit_code"):
        return _text_result(str(result.get("error") or "page_prune failed"))
    return _text_result(json.dumps({"ok": True, "stats": result.get("stats"),
                                    "output": result.get("output")}, indent=2, ensure_ascii=False))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
