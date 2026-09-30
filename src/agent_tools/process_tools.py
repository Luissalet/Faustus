"""agent_tools/process_tools.py -- `process_*` tool executors.

Thin wrappers over `src.process_manager`: start a process and get its output or
an opaque handle, read it through a cursor, write to its stdin, stop it, list
the session's handles. Every call identifies the caller from the tool context
(owner, session, disabled tools) so a handle cannot be used from another
session.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return content
    raw = (content or "").strip() if isinstance(content, str) else ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _failed(tool: str, result: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(result)
    out.setdefault("error", f"{tool}: failed")
    if not str(out["error"]).startswith(tool):
        out["error"] = f"{tool}: {out['error']}"
    out["exit_code"] = out.get("exit_code") or 1
    out.pop("ok", None)
    return out


def _ok(result: Dict[str, Any], text: str) -> Dict[str, Any]:
    out = dict(result)
    out.pop("ok", None)
    out["exit_code"] = 0
    out["output"] = text
    return out


def _render_read(r: Dict[str, Any]) -> str:
    head = (f"handle {r['handle']}: {r['state']}"
            + (f", exit code {r['exit_code']}" if r.get("exit_code") is not None else "")
            + (f" ({r['termination_reason']})" if r.get("termination_reason") and r["state"] != "running" else "")
            + f". cursor={r.get('next_cursor')}"
            + (f", {r['dropped_bytes']} bytes dropped before this cursor" if r.get("dropped_bytes") else "")
            + (", more output is buffered" if r.get("more") else ""))
    if r.get("outcome_unknown"):
        head += ". The outcome is UNKNOWN: read the effects before retrying."
    return head + "\n" + (r.get("output") or "(no new output)")


class ProcessStartTool:
    async def execute(self, content: Any, ctx: Optional[dict]) -> dict:
        from src.process_manager import Caller, manager
        from src.tool_execution import agent_cwd
        args = _args(content)
        command = args.get("command") if isinstance(args.get("command"), str) else None
        argv = args.get("argv") if isinstance(args.get("argv"), list) else None
        result = await asyncio.to_thread(
            manager().start, command, Caller.from_ctx(ctx), argv=argv, cwd=args.get("cwd") or agent_cwd(),
            shell=str(args.get("shell") or "bash"), yield_ms=_int(args.get("yield_ms"), 1500) or 0,
            max_runtime_s=_int(args.get("max_runtime_seconds")),
            env=args.get("env") if isinstance(args.get("env"), dict) else None)
        if not result.get("ok"):
            return _failed("process_start", result)
        head = ("finished" if result.get("completed") else "still running; use process_read with the cursor")
        text = _render_read(result)
        return _ok(result, f"process_start: {head}\n{text}")


class ProcessReadTool:
    async def execute(self, content: Any, ctx: Optional[dict]) -> dict:
        from src.process_manager import Caller, manager
        args = _args(content)
        handle = str(args.get("handle") or "")
        if not handle:
            return {"error": "process_read: a handle is required", "exit_code": 1}
        result = await asyncio.to_thread(
            manager().read, handle, Caller.from_ctx(ctx), cursor=_int(args.get("cursor")),
            max_bytes=_int(args.get("max_bytes"), 16384) or 16384, wait_ms=_int(args.get("wait_ms"), 0) or 0)
        if not result.get("ok"):
            return _failed("process_read", result)
        return _ok(result, _render_read(result))


class ProcessWriteStdinTool:
    async def execute(self, content: Any, ctx: Optional[dict]) -> dict:
        from src.process_manager import Caller, manager
        args = _args(content)
        handle = str(args.get("handle") or "")
        if not handle:
            return {"error": "process_write_stdin: a handle is required", "exit_code": 1}
        result = await asyncio.to_thread(
            manager().write_stdin, handle, Caller.from_ctx(ctx), data=str(args.get("data") or ""),
            close_stdin=bool(args.get("close_stdin")))
        if not result.get("ok"):
            return _failed("process_write_stdin", result)
        return _ok(result, f"wrote {result['written']} bytes to {handle}"
                   + ("; stdin closed" if result.get("stdin_closed") else ""))


class ProcessStopTool:
    async def execute(self, content: Any, ctx: Optional[dict]) -> dict:
        from src.process_manager import Caller, manager
        args = _args(content)
        handle = str(args.get("handle") or "")
        if not handle:
            return {"error": "process_stop: a handle is required", "exit_code": 1}
        result = await asyncio.to_thread(manager().stop, handle, Caller.from_ctx(ctx),
                                         reason=str(args.get("reason") or ""))
        if not result.get("ok"):
            return _failed("process_stop", result)
        note = result.get("note") or ("signalled the process tree this manager started"
                                      if result.get("signalled") else "nothing needed signalling")
        return _ok(result, f"{handle}: {result.get('state')}. {note}")


class ProcessListTool:
    async def execute(self, content: Any, ctx: Optional[dict]) -> dict:
        from src.process_manager import Caller, manager
        args = _args(content)
        result = await asyncio.to_thread(
            manager().list, Caller.from_ctx(ctx), include_finished=args.get("include_finished") is not False)
        rows = result.get("handles") or []
        if not rows:
            return _ok(result, "no process handles in this session")
        lines = [f"- {r['handle']} [{r['state']}] {str(r.get('command') or '')[:80]}"
                 + (f" (exit {r['exit_code']})" if r.get("exit_code") is not None else "")
                 + (f" — {r['termination_reason']}" if r.get("termination_reason") and r["state"] != "running" else "")
                 for r in rows]
        return _ok(result, "\n".join(lines))
