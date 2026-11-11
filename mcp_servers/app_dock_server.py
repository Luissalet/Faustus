"""
app_dock_server.py

MCP server for the App Dock (`src/app_dock.py`): the same three tools the chat has, `app_open`, `app_view`
and `app_act`, for any agent that is not the chat itself (a coding agent, a scheduled task, another
Hoard). The dock lives in the running Faustus (the Studio window showing the chat is the host), so this
server is a thin client of its `/api/app-dock/*` routes; every tool takes the `session` (chat id) whose
side panel the app is docked in.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before anything else is used.
* `FAUSTUS_INTERNAL_API_URL` / `FAUSTUS_URL` (default: the local server), `FAUSTUS_TOKEN` / `FAUSTUS_TOKEN_FILE`
  and `FAUSTUS_INTERNAL_TOOL_TOKEN` authenticate the calls, like the other built-in servers.
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

server = Server("app_dock")
_NAMES = ("app_open", "app_view", "app_act")
_SESSION = {"type": "string", "description": "Chat session id whose side panel holds (or will hold) the app"}


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


def _render(name: str, payload: dict) -> str:
    if name == "app_view" and payload.get("ok"):
        from src.agent_tools.app_dock_tools import _render_view
        from src import app_dock
        return _render_view(payload.get("state") or {}, payload.get("result"))
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    """Dispatch, and never raise."""
    args = dict(arguments or {})
    session = str(args.pop("session", "") or "").strip()
    if name not in _NAMES or not session:
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Invalid App Dock request: `session` and a known tool are required")])
    base = (os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=70, trust_env=False) as client:
            if name == "app_open":
                r = await client.post(base + "/api/app-dock/open", headers=_headers(),
                                      json={"session": session, "app": args.get("app", ""), "wait": 4})
                payload, error = r.json(), not r.is_success
            else:
                state = (await client.get(base + "/api/app-dock/state", headers=_headers(), params={"session": session})).json()
                if not state.get("app_id"):
                    payload, error = {"error": "no app is docked in this conversation; call app_open first", "error_class": "app_dock.no_app"}, True
                else:
                    if name == "app_view":
                        tool, targs = "app_read", {"scope": "page" if args.get("scope") == "page" else "screen"}
                    else:
                        from src.agent_tools.app_dock_tools import _GENERIC, _page_args
                        action = str(args.get("action") or "").strip()
                        low = action.lower()
                        tool, targs = (_GENERIC[low], _page_args(low, args)) if low in _GENERIC else (action, args.get("args") or {})
                    r = await client.post(base + "/api/app-dock/call", headers=_headers(),
                                          json={"session": session, "tool": tool, "args": targs})
                    payload = r.json()
                    error = not r.is_success or not payload.get("ok")
                    if name == "app_view" and not error:
                        payload["state"] = state
    except (httpx.HTTPError, ValueError) as exc:
        payload, error = {"error": f"Faustus did not answer ({type(exc).__name__})"}, True
    return CallToolResult(isError=error, content=[TextContent(type="text", text=_render(name, payload))])


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
