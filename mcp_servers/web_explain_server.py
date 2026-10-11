"""web_explain_server.py

MCP server for "how is this feature of a website built?" (src/web_explain): watch a page perform an action in a fresh
headless browser and return evidence -- requests with the code that issued them, DOM changes, the functions that ran
mapped to original modules through published source maps, packages and versions -- split into observed, inferred and
unknown; plus static bundle analysis and mapping of a minified position back to its source.

Same discipline as the other built-in servers: stdout is the JSON-RPC stream (``src/stdio_guard.py`` is raised before
anything prints), the work module is imported on first use, and ``call_tool`` never raises. Nothing is written but the
screenshots under ``DATA_DIR/web_explain``; the browser profile is new on every call (no logins, no cookies kept).
"""

import asyncio
import json
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

server = Server("web_explain")

_URL = {"type": "string", "description": "http(s) URL of the page."}
_ACTIONS = {
    "type": "array",
    "description": "Steps that trigger the feature, in order. Fresh profile with no login: avoid actions that change "
                   "data elsewhere (purchases, posts).",
    "items": {"type": "object", "required": ["type"], "properties": {
        "type": {"type": "string", "enum": ["click", "fill", "hover", "press", "select", "scroll", "wait", "goto"]},
        "target": {"type": "string", "description": "CSS selector or visible text / label / placeholder."},
        "value": {"description": "Text for fill, key for press, option for select, px for scroll, ms for wait."}}},
}
_WAIT = {"type": "integer", "minimum": 0, "maximum": 15000, "description": "Extra wait after the actions, ms; default 1500."}

TOOLS = [
    Tool(name="explain_feature",
         description=("How a feature of a website is built, with evidence: loads the page, performs the actions, and "
                      "returns numbered evidence (requests and the code that issued them, DOM changes, functions that ran "
                      "mapped to original modules, packages, the question's words in the original code) marked observed, "
                      "inferred or unknown. Cite E ids. Cómo está hecha una función de una web. Read-only."),
         inputSchema={"type": "object", "required": ["url", "question"], "properties": {
             "url": _URL, "question": {"type": "string", "description": "What you want to know about the feature."},
             "actions": _ACTIONS, "wait_ms": _WAIT}}),
    Tool(name="observe_web_execution",
         description=("Raw observation of a page doing something: every request with its initiator (mapped to the "
                      "original file), DOM mutations, JS coverage of the actions only, console, errors, scripts with or "
                      "without source maps and bundled packages. Observa la ejecución de una web. Read-only."),
         inputSchema={"type": "object", "required": ["url"], "properties": {
             "url": _URL, "actions": _ACTIONS, "wait_ms": _WAIT}}),
    Tool(name="analyze_web_bundle",
         description=("Static look at a script, or at the scripts of a page, without a browser: source map, first-party "
                      "modules, packages with versions, license banners, and an optional search in the original sources "
                      "(or the minified code when there is no map). Analiza el bundle JS. Read-only."),
         inputSchema={"type": "object", "required": ["url"], "properties": {
             "url": {"type": "string", "description": "Script URL, or page URL to analyse its <script> files."},
             "search": {"type": "string", "description": "Text to find in the code."},
             "max_scripts": {"type": "integer", "minimum": 1, "maximum": 12, "description": "For a page; default 6."}}}),
    Tool(name="trace_web_source_location",
         description=("Map a minified position (script URL, line, column -- from a stack trace or a request initiator) "
                      "back to the original file, line and symbol through the source map, with a snippet. Traza una "
                      "posición minificada a su fuente. Read-only."),
         inputSchema={"type": "object", "required": ["script_url", "line"], "properties": {
             "script_url": {"type": "string", "description": "http(s) URL of the generated script."},
             "line": {"type": "integer", "minimum": 1, "description": "1-based line."},
             "column": {"type": "integer", "minimum": 0, "description": "0-based column; default 0."},
             "context": {"type": "integer", "minimum": 0, "maximum": 20, "description": "Lines around; default 4."}}}),
]


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(TOOLS)


def _actions(a: dict):
    acts = a.get("actions")
    return acts if isinstance(acts, list) else None


def _dispatch(name: str, a: dict):
    from src import web_explain as wx

    if name == "explain_feature":
        return wx.explain_feature(str(a["url"]), str(a["question"]), _actions(a), int(a.get("wait_ms", 1500)))
    if name == "observe_web_execution":
        return wx.observe_execution(str(a["url"]), _actions(a), int(a.get("wait_ms", 1500)))
    if name == "analyze_web_bundle":
        return wx.analyze_bundle(str(a["url"]), a.get("search") or None, int(a.get("max_scripts", 6)))
    if name == "trace_web_source_location":
        return wx.trace_location(str(a["script_url"]), int(a["line"]), int(a.get("column", 0)), int(a.get("context", 4)))
    return None


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name not in {t.name for t in TOOLS}:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        result = await asyncio.to_thread(_dispatch, name, args)
    except KeyError as exc:
        return _text_result(f"Error in {name}: missing argument {exc}")
    except Exception as exc:  # noqa: BLE001 - bad arguments and unreachable pages come back as text
        return _text_result(f"Error in {name}: {exc}")
    return _text_result(json.dumps(result, indent=2, ensure_ascii=False, default=str))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
