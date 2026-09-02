"""
workers_server.py — "Faustus workers" MCP server for an OUTSIDE coordinator.

Add it to Claude Desktop / Cowork / Claude Code and the expensive model can
hand mechanical work to Faustus's local workers (Ollama on this machine) and
read back a compact result — it never sees a tool transcript, so its own
tokens go to planning and review:

    {
      "mcpServers": {
        "faustus-workers": {
          "command": "D:/LocalAI/odysseus/venv/Scripts/python.exe",
          "args": ["D:/LocalAI/odysseus/mcp_servers/workers_server.py"],
          "env": {"FAUSTUS_URL": "http://127.0.0.1:7000",
                  "FAUSTUS_API_TOKEN": "ody_..."}      # Settings → API tokens → profile "fable_workers"
        }
      }
    }

Tools: dispatch_workers (start a job), workers_wait (block until done, then
the compact result), workers_status, workers_events, workers_cancel,
workers_list. It talks HTTP to the running Faustus (routes/dispatch_routes.py)
— nothing runs in this process, so a crash here cannot take a worker with it.
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
from mcp.types import Tool, TextContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

server = Server("faustus-workers")

BASE = (os.environ.get("FAUSTUS_URL") or "http://127.0.0.1:7000").rstrip("/")
TOKEN = (os.environ.get("FAUSTUS_API_TOKEN") or "").strip()
_TIMEOUT = 30.0
_MAX_WAIT = 600.0


def _request(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = _TIMEOUT) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — the operator's own server
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:600]
        except Exception:
            pass
        raise RuntimeError(f"Faustus answered HTTP {e.code} for {method} {path}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Faustus is not reachable at {BASE}: {e.reason}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Faustus returned non-JSON for {method} {path}")


def render(job: Dict[str, Any]) -> str:
    """The compact result as text the coordinator can read in one glance."""
    res = job.get("result") or {}
    lines = [f"job {job.get('id')} · {job.get('status')} · {job.get('title') or ''}".rstrip(" ·")]
    if job.get("error"):
        lines.append(f"error: {job['error']}")
    if job.get("workspace"):
        lines.append(f"workspace: {job['workspace']} · model: {job.get('model') or '?'} · {job.get('duration_s')} s")
    if job.get("chat_url"):
        lines.append(f"board: {BASE}{job['chat_url']}")
    prog = job.get("progress") or {}
    if prog and job.get("status") in ("queued", "running"):
        for name, p in prog.items():
            bits = [f"{name}: {p.get('last_event')}"]
            if p.get("round") is not None:
                bits.append(f"round {p['round']}")
            if p.get("last_tool") or p.get("tool"):
                bits.append(f"tool {p.get('last_tool') or p.get('tool')}")
            if p.get("elapsed_s") is not None:
                bits.append(f"{p['elapsed_s']} s")
            if p.get("stalled"):
                bits.append(f"STALLED ({p.get('stall_reason') or '?'})")
            lines.append("  " + " · ".join(str(b) for b in bits))
    for w in res.get("workers") or []:
        head = (f"[{w.get('name')}] {w.get('status')}" + (f" ({w.get('stop_reason')})" if w.get("stop_reason") and w.get("stop_reason") != "complete" else "")
                + f" · {w.get('rounds')} rounds · {w.get('tool_calls')} tools ({w.get('failed_calls')} failed)"
                + f" · {w.get('input_tokens')}/{w.get('output_tokens')} tok")
        lines.append(head)
        if w.get("error"):
            lines.append(f"  error: {w['error']}")
        if w.get("files_changed"):
            lines.append("  changed: " + ", ".join(w["files_changed"][:20]))
        if w.get("static_checks"):
            lines.append(f"  static checks: {json.dumps(w['static_checks'])[:300]}")
        if w.get("git"):
            lines.append(f"  git: {json.dumps(w['git'])[:300]}")
        if w.get("summary"):
            lines.append("  says: " + w["summary"])
    if res.get("lock_conflicts"):
        lines.append("lock conflicts: " + "; ".join(res["lock_conflicts"]))
    if res.get("dropped_tasks"):
        lines.append(f"NOTE: {res['dropped_tasks']} task(s) were not run (max 4 per job) — dispatch them again.")
    t = res.get("totals") or {}
    if t:
        lines.append(f"totals: {t.get('tool_calls')} tool calls, {t.get('rounds')} rounds, "
                     f"{t.get('input_tokens')}/{t.get('output_tokens')} local tokens, {t.get('errors')} errors")
    return "\n".join(lines)


TOOLS: List[Tool] = [
    Tool(
        name="dispatch_workers",
        description=(
            "Hand mechanical work to Faustus's LOCAL workers (Ollama on this machine) and get back a compact "
            "result — never a transcript. Give 1–4 self-contained tasks (each an instruction, optionally files "
            "and a model), the workspace folder they may touch, and whether they may run in parallel. Returns "
            "the job id and the Faustus chat where the control board lives. Then call workers_wait."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array", "minItems": 1, "maxItems": 4,
                    "items": {"anyOf": [
                        {"type": "string"},
                        {"type": "object", "properties": {
                            "instruction": {"type": "string"},
                            "name": {"type": "string"},
                            "files": {"type": "array", "items": {"type": "string"}},
                            "model": {"type": "string"},
                        }, "required": ["instruction"]},
                    ]},
                    "description": "Self-contained tasks; say exactly what 'done' means (tests to pass, files to change).",
                },
                "workspace": {"type": "string", "description": "Absolute folder the workers are confined to."},
                "context": {"type": "string", "description": "Shared context every worker gets (short)."},
                "parallel": {"type": "boolean", "default": True},
                "reviewer": {"type": "boolean", "default": False, "description": "Add a reviewer worker after the others."},
                "model": {"type": "string", "description": "Model on the dispatch endpoint (default: the configured worker model)."},
                "max_rounds": {"type": "integer", "minimum": 3, "maximum": 40},
                "timeout_s": {"type": "integer", "minimum": 60, "maximum": 7200},
            },
            "required": ["tasks"],
        },
    ),
    Tool(
        name="workers_wait",
        description="Block until a dispatched job finishes (up to timeout_s, default 300) and return its compact result.",
        inputSchema={"type": "object", "properties": {
            "job_id": {"type": "string"},
            "timeout_s": {"type": "integer", "minimum": 5, "maximum": 600, "default": 300},
        }, "required": ["job_id"]},
    ),
    Tool(
        name="workers_status",
        description="Current status of a dispatched job (progress per worker while it runs; the compact result when done).",
        inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    Tool(
        name="workers_events",
        description="The control-board events of a job so far (last 400) — for diagnosing a stuck worker.",
        inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    Tool(
        name="workers_cancel",
        description="Stop a dispatched job.",
        inputSchema={"type": "object", "properties": {"job_id": {"type": "string"}}, "required": ["job_id"]},
    ),
    Tool(
        name="workers_list",
        description="Recent dispatched jobs (id, status, title).",
        inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    ),
]


@server.list_tools()
async def list_tools() -> List[Tool]:
    return TOOLS


def _text(s: str) -> List[TextContent]:
    return [TextContent(type="text", text=s)]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    args = arguments or {}
    try:
        if name == "dispatch_workers":
            body = {k: v for k, v in args.items() if v is not None}
            job = await asyncio.to_thread(_request, "POST", "/api/dispatch", body)
            return _text(render(job) + "\n(call workers_wait with this job_id)")
        job_id = str(args.get("job_id") or "").strip()
        if name == "workers_wait":
            t = min(_MAX_WAIT, max(5.0, float(args.get("timeout_s") or 300)))
            job = await asyncio.to_thread(_request, "GET", f"/api/dispatch/{job_id}/wait?timeout={int(t)}", None, t + 15)
            return _text(render(job))
        if name == "workers_status":
            return _text(render(await asyncio.to_thread(_request, "GET", f"/api/dispatch/{job_id}")))
        if name == "workers_events":
            data = await asyncio.to_thread(_request, "GET", f"/api/dispatch/{job_id}/events")
            evs = data.get("events") or []
            lines = [f"job {data.get('id')} · {data.get('status')} · {len(evs)} events"]
            for ev in evs[-80:]:
                lines.append("  " + json.dumps(ev, ensure_ascii=False)[:300])
            return _text("\n".join(lines))
        if name == "workers_cancel":
            data = await asyncio.to_thread(_request, "POST", f"/api/dispatch/{job_id}/cancel", {})
            return _text(f"job {data.get('id')}: {'cancelled' if data.get('cancelled') else 'was not running'} ({data.get('status')})")
        if name == "workers_list":
            data = await asyncio.to_thread(_request, "GET", f"/api/dispatch?limit={int(args.get('limit') or 20)}")
            rows = data.get("jobs") or []
            if not rows:
                return _text("no dispatched jobs yet")
            return _text("\n".join(f"{j.get('id')} · {j.get('status')} · {j.get('title')}" for j in rows))
        return _text(f"unknown tool: {name}")
    except Exception as e:  # noqa: BLE001 — the coordinator needs the reason, not a stack
        return _text(f"Error: {e}")


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
