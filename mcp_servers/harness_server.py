"""harness_server.py

MCP server with three read-only diagnostics over the agent harness, so a
local assistant (or any MCP client) can see what the harness would do without
running a turn:

* ``history_projection`` - give a message list and a wire protocol; get back the
  repairs the canonical projection would make (orphan calls, missing results,
  legacy markers) with a receipt for each, and a compact view of the projected
  history. Calls without a result are shown as explicit "no result" items, never
  removed.
* ``resource_claims`` - give tool calls; get back what each one would claim
  (read or write on a path, tree, workspace, process or external resource) and
  which of them may run together and which are serialised.
* ``paired_bench_reports`` - list the paired harness bench reports (same model,
  before vs after) or read one: verdict, success, seconds, tokens and boundary
  violations per arm and case.

Nothing here writes anything or starts a run. The in-process receipt log and
claim broker of the running app are not reachable from this separate process;
these tools compute from what they are given, and read the reports on disk.

* **stdout is the JSON-RPC stream.** `src/stdio_guard.py` is raised before
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

server = Server("harness")

_MAX_MESSAGES = 400
_MAX_CALLS = 64
_PREVIEW_CHARS = 160


def _text_result(text: str) -> list[TextContent]:
    return [TextContent(type="text", text=text)]


def _json_result(payload: dict) -> list[TextContent]:
    return _text_result(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="history_projection",
            description=(
                "Show what the harness would change in a chat history before sending it to a "
                "model. Give the messages (role/content, assistant tool_calls, tool results with "
                "tool_call_id) and a protocol. Returns one receipt per repair (kind, reason, call "
                "id, tool) and a compact view of the projected messages. A tool call with no "
                "result is kept and shown with an explicit unknown-result item; a result that "
                "arrives late is re-attached to its call; nothing is invented. Muestra que "
                "repararia el proyector de historial. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "messages": {"type": "array", "items": {"type": "object"},
                                 "description": "Chat messages, oldest first."},
                    "protocol": {"type": "string", "enum": ["openai_chat", "anthropic", "ollama", "text_fence"],
                                 "description": "Wire protocol to project for (default openai_chat)."},
                },
                "required": ["messages"],
            },
        ),
        Tool(
            name="resource_claims",
            description=(
                "Show what a set of tool calls would claim and how the dispatcher would group "
                "them. Each call is {tool, content}: content is the tool's JSON arguments as a "
                "string. Returns the claims of each call (read or write on a path, tree, "
                "workspace, process or external resource) and the groups: calls in one group may "
                "run in parallel, a call that conflicts with an earlier one (any write on an "
                "overlapping resource) starts a later group. Shell commands and calls with "
                "unknown effects claim nothing. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "calls": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"tool": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["tool"]}},
                    "workspace": {"type": "string", "description": "Workspace root the paths are relative to."},
                    "scope": {"type": "string", "enum": ["files", "files_and_external", "off"],
                              "description": "Which resources are claimed (default: the setting, normally files)."},
                },
                "required": ["calls"],
            },
        ),
        Tool(
            name="paired_bench_reports",
            description=(
                "List the paired harness bench reports (same model, same tasks, two arms: a git "
                "revision or a settings toggle) or read one. A report holds the verdict "
                "(faster / equivalent / slower / regression / blocked / inconclusive), success "
                "rate, rounds, seconds, tokens and boundary violations per arm and case, and the "
                "recovery scenarios. Without `file` the newest reports are listed. Read-only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file": {"type": "string", "description": "Report file name from the listing."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                              "description": "How many reports to list (default 10)."},
                },
            },
        ),
    ]


def _preview(content) -> str:
    if isinstance(content, list):
        content = " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    text = "" if content is None else str(content)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "..."


def _history_projection(args: dict) -> dict:
    from src import history_projection as hp

    messages = args.get("messages")
    if not isinstance(messages, list):
        return {"error": "messages must be a list"}
    if len(messages) > _MAX_MESSAGES:
        return {"error": f"at most {_MAX_MESSAGES} messages per call"}
    protocol = str(args.get("protocol") or "openai_chat")
    if protocol not in hp.PROTOCOLS:
        return {"error": f"unknown protocol {protocol!r}; expected one of {list(hp.PROTOCOLS)}"}
    proj = hp.project(messages, protocol, record=False)
    view = []
    for m in proj.messages:
        if not isinstance(m, dict):
            continue
        row = {"role": m.get("role"), "content": _preview(m.get("content"))}
        if m.get("tool_calls"):
            row["tool_calls"] = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name")}
                                 for c in m["tool_calls"] if isinstance(c, dict)]
        if m.get("tool_call_id"):
            row["tool_call_id"] = m["tool_call_id"]
        view.append(row)
    kinds: dict = {}
    for r in proj.receipts:
        kinds[r.kind] = kinds.get(r.kind, 0) + 1
    return {"protocol": protocol, "messages_in": len(messages), "messages_out": len(proj.messages),
            "repair_count": len(proj.receipts), "repairs_by_kind": kinds,
            "repairs": proj.receipt_dicts(), "projected": view}


def _resource_claims(args: dict) -> dict:
    from src import resource_claims as rc

    calls = args.get("calls")
    if not isinstance(calls, list):
        return {"error": "calls must be a list"}
    if len(calls) > _MAX_CALLS:
        return {"error": f"at most {_MAX_CALLS} calls per request"}
    scope = str(args.get("scope") or rc.scope_setting())
    if scope not in ("files", "files_and_external", "off"):
        return {"error": f"unknown scope {scope!r}"}
    workspace = args.get("workspace") or None
    rows = []
    groups: list = []
    current = rc.ClaimSet()
    current_ids: list = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict) or not call.get("tool"):
            return {"error": f"call {i} needs a tool name"}
        claims = rc.claims_for_call(str(call["tool"]), str(call.get("content") or ""), workspace, scope)
        rows.append({"index": i, "tool": call["tool"], "claims": [c.label() for c in claims],
                     "claims_nothing": not claims})
        if not current.try_add(claims):
            groups.append(current_ids)
            current, current_ids = rc.ClaimSet(), []
            current.try_add(claims)
        current_ids.append(i)
    if current_ids:
        groups.append(current_ids)
    return {"scope": scope, "calls": rows, "groups": groups,
            "parallel": any(len(g) > 1 for g in groups),
            "serialised": len(groups) > 1}


def _paired_bench_reports(args: dict) -> dict:
    from src.bench import harness_pair as hpair

    name = args.get("file")
    if name:
        name = str(name)
        if os.path.basename(name) != name or not name.endswith(".json"):
            return {"error": "file must be a report file name from the listing"}
        path = os.path.join(hpair.reports_dir(), name)
        try:
            report = hpair.load_report(path)
        except (OSError, ValueError) as exc:
            return {"error": f"cannot read {name}: {exc}"}
        report = dict(report)
        report["runs"] = [{k: r.get(k) for k in ("arm", "case", "repeat", "success", "rounds", "seconds",
                                                 "violations", "detail")} for r in report.get("runs", [])]
        return {"file": name, "summary": hpair.render_text(report), "report": report}
    try:
        limit = max(1, min(50, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    return {"reports": hpair.list_reports(limit), "directory": hpair.reports_dir()}


_TOOLS = {
    "history_projection": _history_projection,
    "resource_claims": _resource_claims,
    "paired_bench_reports": _paired_bench_reports,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch, and never raise -- an exception here would kill the server."""
    handler = _TOOLS.get(name)
    if handler is None:
        return _text_result(f"Unknown tool: {name}")
    args = arguments if isinstance(arguments, dict) else {}
    try:
        return _json_result(await asyncio.to_thread(handler, args))
    except Exception as exc:  # noqa: BLE001
        return _text_result(f"Error in {name}: {type(exc).__name__}: {exc}")


async def run():
    async with stdio_server() as (read_stream, write_stream):
        with stdout_guard():
            await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
