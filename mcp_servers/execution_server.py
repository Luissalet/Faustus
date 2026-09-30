"""execution_server.py

MCP server exposing the execution tools of Faustus to a local assistant:

* ``process_start`` / ``process_read`` / ``process_write_stdin`` /
  ``process_stop`` / ``process_list``: opaque process handles with a bounded,
  cursor-addressed output buffer, a stdin channel and a termination reason
  (``src/process_manager.py``). The handles live in THIS server process and in
  its own store file, separate from the ones the chat agent holds; they are
  scoped to a single synthetic session, so one MCP client cannot use another
  client's handles only by guessing ids when the owner differs.
* ``sandbox_probe``: real allowed/forbidden access checks per operation
  (``src/sandbox_probe.py``).

* **stdout is the JSON-RPC stream.** ``src/stdio_guard.py`` is raised before
  anything else is imported, like the other built-in servers.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from src.stdio_guard import guard as stdout_guard
except Exception:  # pragma: no cover - the server must start regardless
    from contextlib import nullcontext as stdout_guard

server = Server("execution")

OWNER = os.environ.get("ODYSSEUS_MCP_EXECUTION_OWNER", "")
SESSION = "mcp-execution"

if not os.environ.get("FAUSTUS_PROCESS_STORE"):
    try:
        from src.constants import DATA_DIR
        os.environ["FAUSTUS_PROCESS_STORE"] = os.path.join(DATA_DIR, "process_handles_mcp.json")
    except Exception:  # pragma: no cover
        pass


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


_HANDLE = {"handle": {"type": "string", "description": "Handle returned by process_start."}}

_TOOLS = {
    "process_start": ("Start a process and get a handle: output within yield_ms, a cursor, and it keeps "
                      "running after this call. Silent processes stay valid. Refused when command "
                      "confinement is required.",
                      {"command": {"type": "string"}, "argv": {"type": "array", "items": {"type": "string"}},
                       "shell": {"type": "string", "enum": ["bash", "sh", "powershell"]},
                       "cwd": {"type": "string"}, "yield_ms": {"type": "integer"},
                       "max_runtime_seconds": {"type": "integer"}, "env": {"type": "object"}}, []),
    "process_read": ("Read new output of a handle through a cursor; reports dropped bytes when the "
                     "bounded buffer moved on.",
                     {**_HANDLE, "cursor": {"type": "integer"}, "max_bytes": {"type": "integer"},
                      "wait_ms": {"type": "integer"}}, ["handle"]),
    "process_write_stdin": ("Write to a running process's stdin (session/owner and permissions are "
                            "checked on every write).",
                            {**_HANDLE, "data": {"type": "string"}, "close_stdin": {"type": "boolean"}},
                            ["handle"]),
    "process_stop": ("Stop a process by handle; only the tree this server started is signalled.",
                     {**_HANDLE, "reason": {"type": "string"}}, ["handle"]),
    "process_list": ("List this server's process handles with state and termination reason.",
                     {"include_finished": {"type": "boolean"}}, []),
    "sandbox_probe": ("Verify what shell, python, Code Mode, file tools and child processes really "
                      "confine, with allowed and forbidden accesses on throw-away fixtures.",
                      {"operations": {"type": "array",
                                      "items": {"type": "string",
                                                "enum": ["shell", "python", "code_mode", "files", "descendants"]}}},
                      []),
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [Tool(name=name, description=desc,
                 inputSchema={"type": "object", "properties": props, "required": required})
            for name, (desc, props, required) in _TOOLS.items()]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    if name not in _TOOLS:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        from src.agent_tools import TOOL_HANDLERS

        result = await TOOL_HANDLERS[name](json.dumps(args), {"owner": OWNER, "session_id": SESSION})
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")
    if result.get("exit_code"):
        return _text_result(str(result.get("error") or f"{name} failed"))
    payload = {k: v for k, v in result.items() if k not in ("exit_code", "report")}
    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
