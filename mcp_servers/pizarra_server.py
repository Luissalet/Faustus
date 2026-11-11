"""
pizarra_server.py

MCP server for Pizarra, the whiteboard (`src/pizarra.py`): `pizarra_create`, `pizarra_read`, `pizarra_edit` and
`pizarra_arrange`, for any agent that is not the chat itself. The boards live in the running Faustus, so this
server is a thin client of its `/api/pizarra/*` routes; every tool takes the `session` (chat id) the board
belongs to, which is also the window that shows the agent's cursor.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is used.
* `FAUSTUS_INTERNAL_API_URL` / `FAUSTUS_URL`, `FAUSTUS_TOKEN` / `FAUSTUS_TOKEN_FILE` and
  `FAUSTUS_INTERNAL_TOOL_TOKEN` authenticate the calls, like the other built-in servers.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("pizarra")
_NAMES = ("pizarra_create", "pizarra_read", "pizarra_edit", "pizarra_arrange")
_SESSION = {"type": "string", "description": "Chat session id the board belongs to"}


def _schemas() -> dict:
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    out = {}
    for s in FUNCTION_TOOL_SCHEMAS:
        fn = s["function"]
        if fn["name"] in _NAMES:
            params = json.loads(json.dumps(fn["parameters"]))
            params["properties"] = {"session": _SESSION, **params.get("properties", {})}
            params["required"] = ["session", *params.get("required", [])]
            out[fn["name"]] = {"description": fn["description"], "parameters": params}
    return out


@server.list_tools()
async def list_tools() -> list[Tool]:
    schemas = _schemas()
    return [Tool(name=n, description=schemas[n]["description"], inputSchema=schemas[n]["parameters"])
            for n in _NAMES if n in schemas]


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


async def _board_id(client, base: str, session: str, given) -> str:
    if given:
        return str(given)
    r = await client.get(base + "/api/pizarra", headers=_headers(), params={"session": session})
    rows = r.json().get("boards") or []
    if not rows:
        raise LookupError("this chat has no board yet; call pizarra_create")
    return rows[0]["id"]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    args = dict(arguments or {})
    session = str(args.pop("session", "") or "").strip()
    if name not in _NAMES or not session:
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Invalid board request: `session` and a known tool are required")])
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    error = False
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            if name == "pizarra_create":
                r = await client.post(base + "/api/pizarra", headers=_headers(), json={"title": args.get("title", ""), "session": session})
                meta = r.json()
                text = f"Board {meta.get('title')!r} created, id {meta.get('id')}."
                ops = args.get("ops")
                if not ops and isinstance(args.get("items"), list):
                    ops = [{"op": "outline", "items": args["items"], "layout": args.get("layout") or "tree",
                            "kind": args.get("kind") or "sticky", "frame_title": args.get("frame_title")}]
                if ops and meta.get("id"):
                    r2 = await client.post(base + f"/api/pizarra/{meta['id']}/ops", headers=_headers(), json={"ops": ops, "session": session})
                    text += "\n" + json.dumps(r2.json().get("results") or r2.json(), ensure_ascii=False)
                    error = not r2.is_success
                error = error or not r.is_success
            else:
                bid = await _board_id(client, base, session, args.get("id"))
                if name == "pizarra_read":
                    r = await client.get(base + f"/api/pizarra/{bid}", headers=_headers())
                    data = r.json()
                    from src import pizarra
                    text = pizarra.outline(data["doc"], title=data["meta"]["title"], board_id=bid, rev=data["meta"]["rev"], region=args.get("region"))
                    error = not r.is_success
                else:
                    if name == "pizarra_arrange":
                        layout = args.get("layout", "grid")
                        scope = args.get("scope", "all")
                        full = (await client.get(base + f"/api/pizarra/{bid}", headers=_headers())).json()
                        ids = scope if isinstance(scope, list) else [n["id"] for n in full["doc"]["nodes"] if n.get("type") != "group"]
                        op = {"op": "layout", "ids": ids, "layout": layout}
                        if isinstance(scope, str) and scope not in ("all", "selected"):
                            op = {"op": "layout", "frame": scope, "layout": layout}
                        ops = [op]
                    else:
                        ops = args.get("ops")
                    r = await client.post(base + f"/api/pizarra/{bid}/ops", headers=_headers(), json={"ops": ops, "session": session})
                    data = r.json()
                    text = "\n".join(data.get("results") or []) if r.is_success else json.dumps(data, ensure_ascii=False)
                    if data.get("error"):
                        text += "\nStopped: " + data["error"]["message"]
                        error = True
                    error = error or not r.is_success
    except LookupError as exc:
        text, error = str(exc), True
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        text, error = f"Faustus did not answer ({type(exc).__name__})", True
    return CallToolResult(isError=error, content=[TextContent(type="text", text=text)])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
