"""source_authority_server.py

MCP server exposing `source_authority`: what kind of site each cited URL is
(government, university, intergovernmental, vendor docs, paper, code host, wiki,
Q&A, forum, blog, shop, social, video) and whether it is an official one. The
same classification the Research screen shows next to every source
(src/source_authority.py): host only, no network access, no writes, and
`official` is True only on a strong signal.

stdout is the JSON-RPC stream; src/stdio_guard.py is raised first, like the
other built-in servers.
"""

import asyncio
import json
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

server = Server("source_authority")

MAX_URLS = 200


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="source_authority",
            description=(
                "Classify cited sources by their host, without a model or the network. Give urls (a list) and get, "
                "for each, source_type (gov, edu, intergovernmental, docs, paper, code, wiki, qa, forum, blog, "
                "shop, social, video, unknown) and official. official is True only on a strong signal: a "
                ".gov/.mil/.edu domain or its country equivalents (gov.uk, gob.es, ac.uk...), an intergovernmental "
                "body, or a docs./developer. subdomain; a forum, blog, shop or code host is never official. Also "
                "returns how many sources are official and the count per type. Clasifica fuentes por su dominio "
                "y dice si son oficiales. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {"urls": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_URLS,
                                        "description": "URLs of the sources to classify."}},
                "required": ["urls"],
            },
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name != "source_authority":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    args = arguments if isinstance(arguments, dict) else {}
    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        return [TextContent(type="text", text="source_authority needs urls: a non-empty list of URLs")]
    try:
        from src.source_authority import annotate, summarise

        urls = [str(u) for u in urls[:MAX_URLS]]
        items = annotate(urls)
        payload = {"sources": [{"url": i["url"], "source_type": i["source_type"], "official": i["official"]}
                               for i in items],
                   "summary": summarise(urls)}
    except Exception as exc:  # noqa: BLE001
        return [TextContent(type="text", text=f"Error in source_authority: {type(exc).__name__}: {exc}")]
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
