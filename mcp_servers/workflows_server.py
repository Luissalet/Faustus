"""
workflows_server.py — "Faustus workflows" MCP server: saved workflows as tools.

Every workflow you have saved in Faustus, enabled, and that declares an input
schema shows up here as one tool, named `wf_<name>` and described from the
workflow. Its arguments are the workflow's declared inputs, plus three reserved
ones:

* `overrides` — `{node_id: {field: value}}`, per-run overrides of a few node
  settings (prompts, thresholds, a loop's ceilings — never a tool list, a
  recipient or a skill). The tool's own schema lists exactly which fields.
* `wait_seconds` — how long to wait for the run to finish before answering
  with its run id (default 30, at most 300). The run goes on either way.
* `idempotency_key` — calling again with the same key returns the run the
  first call started.

A call starts a run and answers with the run id and, if the run finished or
stopped to wait for somebody within the wait, its result. `workflow_run_status`
is the way back to any run by id.

    {
      "mcpServers": {
        "faustus-workflows": {
          "command": "D:/LocalAI/odysseus/venv/Scripts/python.exe",
          "args": ["D:/LocalAI/odysseus/mcp_servers/workflows_server.py"],
          "env": {"FAUSTUS_URL": "http://127.0.0.1:7000",
                  "FAUSTUS_API_TOKEN": "ody_..."}    # Settings -> API tokens -> profile with agents:dispatch
        }
      }
    }

**Why a server of its own, and not more tools in `workers_server.py`.**
`workers_server` is the coordinator's handle on the worker fleet: a fixed list
of tools, read once. This one's tool list is *data*: it changes whenever a
workflow is saved, enabled or disabled, and it is different for every owner.
Mixing the two would make a fixed-tool server lie about its list, or make a
client that only wants to dispatch workers see (and be able to call) every
published workflow. Keeping them apart also means a person can grant one
without the other (two entries in the client's config, two tokens), and that a
crash here cannot touch a dispatched worker. Like `workers_server`, nothing
runs in this process: it is an HTTP client of the running Faustus
(`routes/workflows_routes.py`, `/api/workflows/published`), which owns the
runs, the claims and the idempotency.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# stdout belongs to the JSON-RPC stream (see workers_server.py).
try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("faustus-workflows")

BASE = (os.environ.get("FAUSTUS_URL") or "http://127.0.0.1:7000").rstrip("/")
TOKEN = (os.environ.get("FAUSTUS_API_TOKEN") or "").strip()
_TIMEOUT = 30.0
_WAIT_MARGIN = 30.0
MAX_OUTPUT_CHARS = 30_000

STATUS_TOOL = Tool(
    name="workflow_run_status",
    description=("Where a workflow run stands: status, each node's state, what it is waiting on "
                 "(an approval, a time, a loop that ran out of budget), what failed and why, and "
                 "its result once it has completed. Works for any run a published workflow tool "
                 "started, by run id."),
    inputSchema={"type": "object", "properties": {
        "run_id": {"type": "string", "description": "The run id a workflow tool returned."}},
        "required": ["run_id"]},
)


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None,
             timeout: float = _TIMEOUT) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - the operator's own server
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:800]
        except Exception:  # noqa: BLE001
            pass
        hint = ""
        if exc.code == 401:
            hint = (" - no FAUSTUS_API_TOKEN in this server's env" if not TOKEN
                    else " - the FAUSTUS_API_TOKEN is not accepted (revoked? another Faustus?)")
        elif exc.code == 403:
            hint = " - the token needs the agents:dispatch scope and an admin owner"
        raise RuntimeError(f"Faustus answered HTTP {exc.code} for {method} {path}: {detail}{hint}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Faustus is not reachable at {BASE}: {getattr(exc, 'reason', exc)}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Faustus returned non-JSON for {method} {path}")


def _tool_from_spec(spec: Dict[str, Any]) -> Tool:
    return Tool(name=str(spec["name"]), description=str(spec.get("description") or ""),
                inputSchema=spec.get("inputSchema") or {"type": "object", "properties": {}})


def fetch_tools() -> List[Tool]:
    """Published workflows as tools, then the status tool. If Faustus cannot
    be reached the list is just the status tool: a client that listed tools
    while Faustus was down still gets a valid answer, and lists again later."""
    tools: List[Tool] = []
    try:
        for spec in (_request("GET", "/api/workflows/published").get("tools") or []):
            tools.append(_tool_from_spec(spec))
    except Exception:  # noqa: BLE001 - see the docstring
        pass
    tools.append(STATUS_TOOL)
    return tools


def render(data: Dict[str, Any]) -> str:
    """One run as compact text a coordinator can act on."""
    body = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    if len(body) > MAX_OUTPUT_CHARS:
        body = body[:MAX_OUTPUT_CHARS] + "\n[truncated]"
    return body


def _text(s: str) -> List[TextContent]:
    return [TextContent(type="text", text=s)]


@server.list_tools()
async def list_tools() -> List[Tool]:
    return await asyncio.to_thread(fetch_tools)


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    args = dict(arguments or {})
    try:
        if name == "workflow_run_status":
            run_id = str(args.get("run_id") or "").strip()
            if not run_id:
                return _text("Error: run_id is required")
            data = await asyncio.to_thread(_request, "GET", f"/api/workflows/published/runs/{run_id}")
            return _text(render(data))
        wait = args.get("wait_seconds")
        timeout = _TIMEOUT + _WAIT_MARGIN + (float(wait) if isinstance(wait, (int, float)) else 30.0)
        data = await asyncio.to_thread(
            _request, "POST", f"/api/workflows/published/{name}/call", {"arguments": args}, timeout)
        return _text(render(data))
    except Exception as exc:  # noqa: BLE001 - the coordinator needs the reason, not a stack
        return _text(f"Error: {exc}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
