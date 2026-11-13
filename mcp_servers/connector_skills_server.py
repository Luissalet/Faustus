"""
connector_skills_server.py

MCP server for the skills the connected apps carry (`src/connector_skills.py`): which skills each
connector brings and the text of one, for any agent that is not the chat itself (a coding agent, a
scheduled task, another Hoard). A thin client of the running Faustus, whose manager holds the live
connector sessions: `GET /api/connector-skills`.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is used.
* `FAUSTUS_INTERNAL_API_URL` / `FAUSTUS_URL` (default: the local server), `FAUSTUS_TOKEN` /
  `FAUSTUS_TOKEN_FILE` and `FAUSTUS_INTERNAL_TOOL_TOKEN` authenticate the calls.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("connector_skills")

_TOOLS = {
    "connector_skills_list": (
        "The skills the connected apps (connectors) carry: for each connector its skills with slug, what each is for "
        "and when to use it (the 'Use when...' line), version and whether the skill is also in Faustus's library. "
        "Collected when a connector connects or its tools are refreshed. Give `server_id` for one connector; "
        "`refresh` reads them again from the app.",
        {"type": "object", "properties": {
            "server_id": {"type": "string", "description": "MCP server id of one connector (empty = every connected connector with skills)."},
            "refresh": {"type": "boolean", "description": "Ask the app again instead of using what was collected.", "default": False}}},
    ),
    "connector_skill_read": (
        "The full text of one skill a connector carries (procedure, pitfalls, how to verify), read from the app's own "
        "shelf. `file` names one of its reference files; empty reads the procedure itself.",
        {"type": "object", "required": ["server_id", "slug"], "properties": {
            "server_id": {"type": "string", "description": "MCP server id of the connector, from connector_skills_list."},
            "slug": {"type": "string", "description": "The skill's slug."},
            "file": {"type": "string", "description": "A reference file of the skill (empty = the procedure)."}}},
    ),
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=n, description=d, inputSchema=s) for n, (d, s) in _TOOLS.items()]


def _headers() -> dict:
    headers = {}
    token = os.environ.get("FAUSTUS_TOKEN", "")
    if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
        try:
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if token:
        headers["Authorization"] = "Bearer " + token
    if os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN"):
        headers["X-Odysseus-Internal-Token"] = os.environ["FAUSTUS_INTERNAL_TOOL_TOKEN"]
    return headers


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    args = dict(arguments or {})
    if name not in _TOOLS:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=f"Unknown tool: {name}")])
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    server_id = str(args.get("server_id") or "").strip()
    try:
        async with httpx.AsyncClient(timeout=70, trust_env=False) as client:
            if name == "connector_skill_read":
                slug = str(args.get("slug") or "").strip()
                if not server_id or not slug:
                    return CallToolResult(isError=True, content=[TextContent(type="text", text="`server_id` and `slug` are required")])
                r = await client.get(f"{base}/api/connector-skills/{quote(server_id, safe='')}/{quote(slug, safe='')}",
                                     headers=_headers(), params={"file": str(args.get("file") or "")})
            elif server_id:
                r = await client.get(f"{base}/api/connector-skills/{quote(server_id, safe='')}", headers=_headers(),
                                     params={"refresh": 1 if args.get("refresh") else 0})
            else:
                r = await client.get(f"{base}/api/connector-skills", headers=_headers(),
                                     params={"refresh": 1 if args.get("refresh") else 0})
            try:
                payload = r.json()
            except ValueError:
                payload = {"error": r.text[:400]}
            error = not r.is_success
    except httpx.HTTPError as exc:
        payload, error = {"error": f"Faustus did not answer ({type(exc).__name__})"}, True
    return CallToolResult(isError=error, content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2, default=str))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
