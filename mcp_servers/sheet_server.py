"""
sheet_server.py

MCP server for Hoja, the spreadsheet (`src/hoja.py`): `sheet_create`, `sheet_read`, `sheet_write` and `sheet_query`,
for any agent that is not the chat itself. The workbooks live in the running Faustus, so this server is a thin client
of its `/api/hoja/*` routes; every tool takes the `session` (chat id) the sheet belongs to, which is also the window
that shows the agent's cursor.

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

server = Server("sheet")
_NAMES = ("sheet_create", "sheet_read", "sheet_write", "sheet_query")
_SESSION = {"type": "string", "description": "Chat session id the sheet belongs to"}


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


async def _sheet_id(client, base: str, session: str, given) -> str:
    if given:
        return str(given)
    r = await client.get(base + "/api/hoja", headers=_headers(), params={"session": session})
    rows = r.json().get("sheets") or []
    if not rows:
        raise LookupError("this chat has no spreadsheet yet; call sheet_create")
    return rows[0]["id"]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    args = dict(arguments or {})
    session = str(args.pop("session", "") or "").strip()
    if name not in _NAMES or not session:
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Invalid sheet request: `session` and a known tool are required")])
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    error = False
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            if name == "sheet_create":
                r = await client.post(base + "/api/hoja", headers=_headers(),
                                      json={"title": args.get("title", ""), "session": session, "csv": args.get("csv"), "tab": args.get("tab")})
                meta = r.json()
                text = f"Sheet {meta.get('title')!r} created, id {meta.get('id')}."
                ops = args.get("ops")
                if not ops and isinstance(args.get("rows"), list) and args["rows"]:
                    ops = [{"op": "set", "at": "A1", "values": args["rows"]}]
                if ops and meta.get("id"):
                    r2 = await client.post(base + f"/api/hoja/{meta['id']}/ops", headers=_headers(), json={"ops": ops, "session": session})
                    text += "\n" + json.dumps(r2.json().get("results") or r2.json(), ensure_ascii=False)
                    error = not r2.is_success
                error = error or not r.is_success
            else:
                sid = await _sheet_id(client, base, session, args.get("id"))
                if name == "sheet_read":
                    r = await client.get(base + f"/api/hoja/{sid}/read", headers=_headers(),
                                         params={"session": session, "tab": args.get("tab") or "", "range": args.get("range") or ""})
                    data = r.json()
                    text = data.get("text") or json.dumps(data, ensure_ascii=False)
                    error = not r.is_success
                elif name == "sheet_query":
                    spec = {k: v for k, v in args.items() if k != "id"}
                    r = await client.post(base + f"/api/hoja/{sid}/query", headers=_headers(), json={"session": session, "spec": spec, "tab": args.get("tab")})
                    data = r.json()
                    text = data.get("text") or json.dumps(data, ensure_ascii=False)
                    error = not r.is_success
                else:
                    r = await client.post(base + f"/api/hoja/{sid}/ops", headers=_headers(), json={"ops": args.get("ops"), "session": session})
                    data = r.json()
                    text = "\n".join(data.get("results") or []) if r.is_success else json.dumps(data, ensure_ascii=False)
                    if data.get("error"):
                        text += "\nStopped: " + (data["error"].get("message") if isinstance(data["error"], dict) else str(data["error"]))
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
