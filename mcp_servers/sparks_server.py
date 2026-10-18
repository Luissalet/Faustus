"""Native Sparks model controls through Faustus's own API."""
import asyncio
import json
import os
from pathlib import Path
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool, ToolAnnotations

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.constants import internal_api_base
from src.stdio_guard import guard

server = Server("sparks")
S = {"type": "string"}
# Native deployment waits include the controller operation and metadata sync.
DEPLOY_TIMEOUT_S = {"start": 120.0, "stop": 960.0}
SPECS = {
    "sparks_status": ("GET", "/status", {"recipes": {"type": "boolean"}}, [], True,
        "Read the configured global Sparks backend, available cluster models, recipes and effective model, including an unavailable service."),
    "sparks_sync": ("POST", "/sync", {}, [], False,
        "Refresh Faustus model endpoints from its configured Sparks controller. Preserve the selected global backend if unavailable; never select a PC text-model fallback."),
    "sparks_settings": ("PUT", "/settings", {"enabled": {"type": "boolean"}, "url": S, "default_backend": {"type": "boolean"}, "recipe": S, "first_token_timeout_s": {"type": "number"}}, [], False,
        "Update native Sparks preferences and synchronize. Disabling automatic selection keeps the current choice. An unavailable service does not change to a PC text model."),
    "sparks_deploy": ("POST", "/deploy", {"recipe": S, "action": {"type": "string", "enum": ["start", "stop"]}, "stop_conflicts": {"type": "boolean"}}, ["recipe", "action"], False,
        "Use the existing native controller to start or stop an explicitly requested Spark recipe. It can alter resident models; inspect current status first and preserve the global service unless the user requested the change."),
}


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=spec[5], inputSchema={"type": "object", "properties": spec[2], "required": spec[3]},
                 annotations=ToolAnnotations(readOnlyHint=spec[4], destructiveHint=False),
                 **({"x-timeout-s": DEPLOY_TIMEOUT_S["stop"] + 15} if name == "sparks_deploy" else {}))
            for name, spec in SPECS.items()]


@server.call_tool()
async def call_tool(name, arguments):
    if name not in SPECS:
        return [TextContent(type="text", text=json.dumps({"error": "Unknown Sparks tool", "exit_code": 1}))]
    method, path, *_ = SPECS[name]
    args = dict(arguments or {})
    try:
        base = os.environ.get("FAUSTUS_INTERNAL_API_URL") or os.environ.get("FAUSTUS_URL") or internal_api_base()
        headers = {}
        token = os.environ.get("FAUSTUS_TOKEN", "")
        if not token and os.environ.get("FAUSTUS_TOKEN_FILE"):
            token = Path(os.environ["FAUSTUS_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        internal = os.environ.get("FAUSTUS_INTERNAL_TOOL_TOKEN", "")
        if internal:
            headers["X-Odysseus-Internal-Token"] = internal
        timeout = DEPLOY_TIMEOUT_S.get(str(args.get("action")), 40) if name == "sparks_deploy" else 40
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.request(method, base.rstrip("/") + "/api/sparks" + path, headers=headers,
                **({"params": args} if method == "GET" else {"json": args}))
        payload = response.json()
        if not response.is_success:
            payload = {"exit_code": 1, "status_code": response.status_code, "error": payload}
    except (httpx.HTTPError, ValueError, OSError) as error:
        payload = {"exit_code": 1, "error": "Sparks API request failed: " + type(error).__name__}
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]


async def main():
    async with stdio_server() as (read, write):
        with guard():
            await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
