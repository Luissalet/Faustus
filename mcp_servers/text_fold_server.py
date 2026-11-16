"""Read a long text or stored tool result one level of detail at a time, through the running Faustus."""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from mcp_servers.app_dock_server import _headers
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("text_fold")
_LEVEL = {"type": "integer", "minimum": 1, "maximum": 5,
          "description": "1 full text, 2 literal key sentences (no model), 3 idea and reason per paragraph, "
                         "4 one line per section, 5 topic cards. Levels 3-5 are generated on demand and cached."}
_TOOLS = [
    Tool(name="fold_text", description=(
        "Fold a long text, or a stored tool result (artifact_id), into a document of numbered paragraphs and sections "
        "(format faustus.fold/1) and return its outline: sections, sizes per level. Optionally read one level at once. "
        "Use it instead of pulling a long text whole into the context."),
        inputSchema={"type": "object", "properties": {
            "text": {"type": "string"}, "artifact_id": {"type": "string", "description": "A stored (offloaded) tool result."},
            "text_pointer": {"type": "string", "description": "RFC 6901 pointer to the string to fold inside a JSON artifact; default: its longest string."},
            "level": _LEVEL, "section": {"type": "string"}},
            "additionalProperties": False}),
    Tool(name="fold_read", description=(
        "Read one level of a folded document: all of it, one section (s1, s2...), or the original text of one "
        "paragraph (p1, p2...) to open what a folded fragment came from. Takes the id from fold_text, or an "
        "artifact_id / text to fold and read in one call. If the model is unavailable, levels 3-5 fail and levels 1-2 still work."),
        inputSchema={"type": "object", "properties": {
            "id": {"type": "string"}, "artifact_id": {"type": "string"}, "text": {"type": "string"}, "text_pointer": {"type": "string"},
            "level": _LEVEL, "section": {"type": "string"}, "paragraph": {"type": "string"}},
            "additionalProperties": False}),
]


@server.list_tools()
async def list_tools():
    return _TOOLS


@server.call_tool()
async def call_tool(name, arguments):
    args = {k: v for k, v in dict(arguments or {}).items() if v not in (None, "")}
    base = (os.getenv("FAUSTUS_INTERNAL_API_URL") or os.getenv("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    headers = _headers()
    owner = os.getenv("ODYSSEUS_MCP_FOLD_OWNER", "")
    if owner:
        headers["X-Odysseus-Owner"] = owner
    try:
        async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
            if name == "fold_text":
                response = await client.post(base + "/api/fold", headers=headers, json=args)
            elif name == "fold_read":
                fold_id = args.pop("id", "")
                if fold_id:
                    query = urlencode({k: v for k, v in args.items() if k in ("level", "section", "paragraph")})
                    response = await client.get(f"{base}/api/fold/{quote(str(fold_id), safe='')}?{query}", headers=headers)
                elif args.get("text") or args.get("artifact_id"):
                    response = await client.post(base + "/api/fold", headers=headers, json={"level": 1, **args})
                else:
                    raise ValueError("give an id, an artifact_id or a text")
            else:
                raise ValueError("Unknown fold tool")
            body = response.text
            if name == "fold_read" and response.is_success:
                data = response.json()
                body = json.dumps(data.get("view") or data, ensure_ascii=False)
            return CallToolResult(isError=not response.is_success, content=[TextContent(type="text", text=body)])
    except Exception as exc:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=str(exc))])


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
