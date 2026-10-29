"""Stdio MCP builtin for the existing browser session. Never launches a browser."""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool, ToolAnnotations

from src.browser_relay.protocol import SCHEMA, failure, validate_arguments
from src.browser_relay.presentation import present_payload
from src.browser_relay.transport import BrowserRelay
from src.stdio_guard import guard


def mcp_result(payload: dict, detail: str = "compact", view: dict | None = None) -> CallToolResult:
    # The wire image appears once as an MCP image, never again in text/structuredContent.
    payload = dict(present_payload(payload, detail, view))
    images = []
    if payload.get("ok") and "image" in payload.get("result", {}):
        result = dict(payload["result"])
        image = result.pop("image")
        try:
            if not isinstance(image, dict) or image.get("mimeType") != "image/png":
                raise ValueError("Expected a PNG image")
            data = image["data"]
            if not isinstance(data, str) or not base64.b64decode(data, validate=True).startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError("Invalid PNG image")
        except (KeyError, TypeError, ValueError, binascii.Error):
            payload = failure("invalid_screenshot", "Extension returned an invalid PNG image", "not_dispatched")
        else:
            images.append(ImageContent(type="image", data=data, mimeType="image/png"))
            payload["result"] = result
    return CallToolResult(isError=not payload["ok"], structuredContent=payload,
                          content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False)), *images])


def create_server(relay: BrowserRelay) -> Server:
    server = Server("browser_session")

    @server.list_tools()
    async def list_tools():
        return [Tool(name="browser_session", description=(
            "Use the existing browser session through its installed extension. tabs lists real tabs; "
            "all other actions require an explicit tab_id. Use refs from the latest snapshot for click/type; "
            "press optionally focuses ref. type inserts Unicode text. Mutations return automatic readback. "
            "release detaches this tool without closing the tab. Main-frame snapshots only. "
            "detail defaults to compact (original refs and useful text/controls); full returns the complete AX graph. "
            "For snapshot or mutation readback, optional view selects flat original AX rows: query is a literal "
            "case-insensitive name/value substring; roles are case-sensitive, e.g. StaticText or button, and combine with query. "
            "offset defaults to 0; limit, if given, is at least 1; omitted means all matches. "
            "selection reports total, matches and next_offset; matched_roles diagnoses roles that eliminate all query matches. "
            "For split text use view:{query:'visibles',roles:['StaticText'],context:true}: context_nodes adds original "
            "direct parents and siblings, while nodes remains flat. No ancestry is invented for null refs. "
            "An outcome_unknown must be observed before deciding any explicit retry; never replay it automatically."),
            inputSchema=SCHEMA, annotations=ToolAnnotations(readOnlyHint=False, idempotentHint=False))]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        if name != "browser_session":
            return mcp_result(failure("unknown_tool", f"Unknown tool: {name}"))
        try:
            args = validate_arguments(arguments)
        except ValueError as exc:
            return mcp_result(failure("invalid_arguments", str(exc)))
        detail = args.pop("detail", "compact")
        view = args.pop("view", None)
        return mcp_result(await relay.call(args), detail, view)

    return server


async def main(port: int = 7016, timeout: float = 30):
    relay = BrowserRelay(port=port, timeout=timeout)
    await relay.start()
    # Also permits tests to request port0 without reserving a racy candidate port.
    print(f"browser_relay_listening=127.0.0.1:{relay.port}", file=sys.stderr, flush=True)
    try:
        server = create_server(relay)
        async with stdio_server() as (read, write):
            with guard(force=True):
                await server.run(read, write, server.create_initialization_options())
    finally:
        await relay.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7016)
    parser.add_argument("--timeout", type=float, default=30)
    options = parser.parse_args()
    try:
        asyncio.run(main(options.port, options.timeout))
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
