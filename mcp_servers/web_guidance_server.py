"""
web_guidance_server.py

MCP server for the vendored modern web guidance (`src/web_guidance.py`): search the guides for a web
platform capability, read one, list them. The guides are files shipped with Faustus, so this works
without the app running and without any network; a coding agent that is not the chat (Claude Code,
Codex, a scheduled task) reads the same guides the chat does before it writes web UI code.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is used.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import web_guidance
from src.stdio_guard import guard

server = Server("web_guidance")

_TOOLS = {
    "web_guidance_search": (
        "Search the modern web platform guides (dialogs and popovers, anchor positioning, view transitions, "
        "scroll-driven effects, forms and autofill, accessibility, performance, security, passkeys, web "
        "components, built-in AI...) with a 2-5 word English query naming ONE capability, e.g. 'light dismiss "
        "dialog' or 'responsive data table'. Returns ids with a one-line description. Run it before writing "
        "HTML/CSS or client-side JS: the guides hold the current pattern and the fallbacks it needs.",
        {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "2-5 words, one capability; no framework or product names."},
            "limit": {"type": "integer", "description": "How many guides (default 8).", "default": 8},
            "category": {"type": "string", "description": "Only this category (css, forms, ui-components, ...)."}}},
    ),
    "web_guidance_retrieve": (
        "The full text of one or more guides by id (comma-separated), from web_guidance_search results. Add the "
        "discipline guide that applies (html, css, forms, accessibility, performance, security). A long guide comes "
        "back in slices: when `truncated` is true, call again with `offset` = `next_offset`.",
        {"type": "object", "required": ["ids"], "properties": {
            "ids": {"type": "string", "description": "Guide id, or several separated by commas."},
            "offset": {"type": "integer", "description": "Character to continue from.", "default": 0},
            "max_chars": {"type": "integer", "description": "Slice size (default 24000).", "default": 24000}}},
    ),
    "web_guidance_list": (
        "Every guide with its description, or one category's (accessibility, built-in-ai, css, forms, html, js, "
        "performance, privacy, pwa, security, ui-atoms, ui-behaviors, ui-components, user-experience, "
        "visual-design, wasm, webmcp); also says which version is vendored.",
        {"type": "object", "properties": {
            "category": {"type": "string", "description": "One category (empty = all)."}}},
    ),
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=n, description=d, inputSchema=s) for n, (d, s) in _TOOLS.items()]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    args = dict(arguments or {})
    if name == "web_guidance_search":
        payload = web_guidance.search(str(args.get("query") or ""), args.get("limit") or web_guidance.DEFAULT_LIMIT,
                                      args.get("category"))
    elif name == "web_guidance_retrieve":
        payload = web_guidance.retrieve(args.get("ids"), args.get("offset") or 0,
                                        args.get("max_chars") or web_guidance.DEFAULT_MAX_CHARS)
    elif name == "web_guidance_list":
        payload = {**web_guidance.list_guides(args.get("category")), "vendored": web_guidance.status()}
    else:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=f"Unknown tool: {name}")])
    return CallToolResult(isError=False, content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
