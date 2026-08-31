"""subagent_tools.py — multi-agent delegation from inside a chat.

`delegate_agents` lets the agent (or the user, via `/agents`) split work into
independent sub-tasks, each run by its own agent loop in its own child chat
session, with the same workspace confinement and the same reliability harness
(src/agent_harness.py). The parent receives a report built from EVIDENCE —
files each sub-agent really changed, tool counts, stop reason — never from the
sub-agent's own prose alone.

Why child sessions: every sub-agent's transcript stays browsable in the sidebar
(folder "Agents"), so the user can audit what each worker actually did.

Progress is streamed live to the parent chat through the tool progress
callback: each payload carries {"subagent": {...}} and the UI renders one card
per worker (static/js/agentHarnessUI.js).

Local backends (Ollama) serve one request at a time per model, so "parallel"
here means concurrent harness loops; the GPU still serializes generations.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SUBAGENTS = 4
DEFAULT_MAX_ROUNDS = 14
SUBAGENT_FOLDER = "Agents"
# Tools a worker must not use: no recursion, no user prompts (nobody is
# watching the child chat), no session management.
SUBAGENT_DISABLED_TOOLS = frozenset({
    "delegate_agents", "ask_user", "create_session", "send_to_session",
    "manage_session", "list_sessions", "pipeline", "chat_with_model",
    "ask_teacher", "update_plan",
})


def _short(text: Any, n: int = 160) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_delegation_args(content: str) -> Dict[str, Any]:
    """Accept {"tasks":[{"name","instruction"}...], "parallel": bool,
    "max_rounds": int} — or, leniently, a plain list of instruction strings."""
    raw = (content or "").strip()
    if not raw:
        raise ValueError("delegate_agents: JSON object with a 'tasks' list is required")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"delegate_agents: arguments must be JSON ({e})")
    if isinstance(data, list):
        data = {"tasks": data}
    if not isinstance(data, dict):
        raise ValueError("delegate_agents: arguments must be a JSON object")
    tasks_raw = data.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ValueError("delegate_agents: 'tasks' must be a non-empty list")
    tasks: List[Dict[str, str]] = []
    for i, t in enumerate(tasks_raw[:MAX_SUBAGENTS]):
        if isinstance(t, str):
            instruction, name = t.strip(), ""
        elif isinstance(t, dict):
            instruction = str(t.get("instruction") or t.get("task") or t.get("content") or "").strip()
            name = str(t.get("name") or t.get("title") or "").strip()
        else:
            continue
        if not instruction:
            continue
        if not name:
            name = _short(instruction, 48)
        tasks.append({"name": name[:80], "instruction": instruction[:8000], "model": str(t.get("model") or "").strip() if isinstance(t, dict) else ""})
    if not tasks:
        raise ValueError("delegate_agents: no usable tasks (each needs an 'instruction')")
    if len(tasks_raw) > MAX_SUBAGENTS:
        logger.info("delegate_agents: %s tasks requested, capped at %s", len(tasks_raw), MAX_SUBAGENTS)
    try:
        max_rounds = int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    except (TypeError, ValueError):
        max_rounds = DEFAULT_MAX_ROUNDS
    return {
        "tasks": tasks,
        "parallel": bool(data.get("parallel", True)),
        "max_rounds": max(3, min(max_rounds, 40)),
        "shared_context": str(data.get("context") or data.get("shared_context") or "").strip()[:4000],
    }


class SubagentRun:
    def __init__(self, index: int, task: Dict[str, str]):
        self.index = index
        self.id = f"sa{index + 1}-{uuid.uuid4().hex[:6]}"
        self.name = task["name"]
        self.instruction = task["instruction"]
        self.model_override = task.get("model") or ""
        self.session_id: Optional[str] = None
        self.text = ""
        self.tool_calls = 0
        self.failed_calls = 0
        self.mutations: List[str] = []
        self.rejections = 0
        self.stop_reason = "unknown"
        self.error: Optional[str] = None
        self.started = time.time()
        self.finished: Optional[float] = None
        self.static_checks: List[Dict[str, Any]] = []
        self.git: Optional[Dict[str, Any]] = None
        self.rounds = 0
        self.summary: Optional[Dict[str, Any]] = None

    def report(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "session_id": self.session_id,
            "status": "error" if self.error else ("done" if self.stop_reason in ("complete",) else self.stop_reason),
            "stop_reason": self.stop_reason, "error": self.error,
            "tool_calls": self.tool_calls, "failed_calls": self.failed_calls,
            "mutations": self.mutations, "rejections": self.rejections, "rounds": self.rounds,
            "static_checks": self.static_checks, "git": self.git,
            "duration_s": round((self.finished or time.time()) - self.started, 1),
            "final_text": self.text.strip()[:2500],
        }


async def _run_subagent(
    run: SubagentRun,
    *,
    endpoint_url: str,
    model: str,
    headers: Optional[Dict],
    owner: Optional[str],
    workspace: Optional[str],
    workspace_roots: Optional[List[str]],
    max_rounds: int,
    shared_context: str,
    parent_session_id: Optional[str],
    emit: Callable[[Dict[str, Any]], Awaitable[None]],
    gen_overrides: Optional[Dict] = None,
) -> None:
    from src.agent_loop import stream_agent_loop
    from src.ai_interaction import get_session_manager
    from core.models import ChatMessage

    sm = get_session_manager()
    parent_name = ""
    if sm and parent_session_id:
        try:
            parent = sm.get_session(parent_session_id)
            parent_name = getattr(parent, "name", "") or ""
        except Exception:
            parent_name = ""
    child_sid = str(uuid.uuid4())[:8]
    run.session_id = child_sid
    if sm:
        try:
            sm.create_session(
                session_id=child_sid,
                name=f"🤖 {run.name}" + (f" — {_short(parent_name, 40)}" if parent_name else ""),
                endpoint_url=endpoint_url, model=model, rag=False, owner=owner,
            )
            child = sm.get_session(child_sid)
            if child is not None:
                if headers:
                    child.headers = headers
                try:
                    child.folder = SUBAGENT_FOLDER
                except Exception:
                    pass
                try:
                    child.mode = "agent"
                except Exception:
                    pass
        except Exception as e:
            logger.warning("delegate_agents: child session creation failed: %s", e)

    preamble = (
        "You are a sub-agent working on ONE delegated task inside a larger job. "
        "Work only on this task, in the shared workspace, using tools. Do not ask "
        "the user questions — decide and act. Finish with a short factual report of "
        "what you changed (files) and what you verified."
    )
    if shared_context:
        preamble += "\n\nShared context from the coordinator:\n" + shared_context
    messages = [{"role": "user", "content": f"{preamble}\n\nYOUR TASK: {run.instruction}"}]

    await emit({"event": "started", "name": run.name, "instruction": _short(run.instruction, 240), "session_id": child_sid})
    final_metrics: Dict[str, Any] = {}
    tool_events: List[Dict[str, Any]] = []
    try:
        async for chunk in stream_agent_loop(
            endpoint_url, model, messages,
            headers=headers, temperature=0.3, max_tokens=0,
            max_rounds=max_rounds, session_id=child_sid, owner=owner,
            workspace=workspace, workspace_roots=workspace_roots,
            disabled_tools=set(SUBAGENT_DISABLED_TOOLS),
            security_gate_bypass=True, _is_teacher_run=True,
            gen_overrides=gen_overrides,
        ):
            if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
                if chunk.startswith("event: error"):
                    run.error = _short(chunk, 300)
                    await emit({"event": "error", "message": run.error})
                continue
            try:
                ev = json.loads(chunk[6:])
            except json.JSONDecodeError:
                continue
            et = ev.get("type")
            if "delta" in ev and not et:
                if not ev.get("thinking"):
                    run.text += ev["delta"]
                continue
            if et == "tool_start":
                await emit({"event": "tool", "tool": ev.get("tool"), "command": _short(ev.get("command"), 120), "phase": "start"})
            elif et == "tool_output":
                run.tool_calls += 1
                ok = ev.get("exit_code") in (0, None)
                if not ok:
                    run.failed_calls += 1
                tool_events.append({"tool": ev.get("tool"), "command": ev.get("command"), "output": _short(ev.get("output"), 400), "exit_code": ev.get("exit_code")})
                await emit({"event": "tool", "tool": ev.get("tool"), "ok": ok, "phase": "done", "output": _short(ev.get("output"), 120)})
            elif et == "round_info":
                run.rounds = max(run.rounds, int(ev.get("round") or 0))
            elif et == "harness_check":
                if ev.get("status") in ("rejected", "syntax_error"):
                    run.rejections += 1
                await emit({"event": "harness", "status": ev.get("status"), "reasons": ev.get("reasons"), "bad_paths": ev.get("bad_paths")})
            elif et == "harness_summary":
                d = ev.get("data") or {}
                run.summary = d
                run.mutations = list(d.get("mutations") or [])
                run.stop_reason = d.get("stop_reason") or run.stop_reason
                run.static_checks = list(d.get("static_checks") or [])
                run.git = d.get("git")
            elif et == "metrics":
                final_metrics = ev.get("data") or {}
                if run.stop_reason == "unknown":
                    run.stop_reason = ((final_metrics.get("harness") or {}).get("stop_reason")) or "complete"
            elif et in ("rounds_exhausted", "budget_exceeded", "loop_breaker_triggered", "intent_nudge_exhausted"):
                await emit({"event": "guard", "kind": et})
            elif et == "agent_terminal":
                run.error = "model request failed"
                await emit({"event": "error", "message": run.error})
    except Exception as e:
        run.error = f"{type(e).__name__}: {e}"[:300]
        logger.warning("delegate_agents: sub-agent %s crashed: %s", run.name, e, exc_info=True)
        await emit({"event": "error", "message": run.error})
    finally:
        run.finished = time.time()
        if run.stop_reason == "unknown":
            run.stop_reason = "error" if run.error else "complete"
    # Persist the transcript into the child chat so it can be audited later.
    if sm and run.session_id:
        try:
            child = sm.get_session(run.session_id)
            if child is not None:
                child.add_message(ChatMessage("user", run.instruction))
                meta = dict(final_metrics or {})
                meta["tool_events"] = tool_events[:60]
                meta["subagent"] = {"parent_session": parent_session_id, "name": run.name}
                child.add_message(ChatMessage("assistant", run.text.strip() or "(no final text)", metadata=meta))
                sm.save_sessions()
        except Exception as e:
            logger.debug("delegate_agents: transcript save failed: %s", e)
    await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300)})


def _build_report_text(runs: List[SubagentRun], workspace: Optional[str]) -> str:
    lines = [f"Delegated {len(runs)} sub-agent task(s). Evidence-based report (from tool logs, not from the workers' prose):"]
    for r in runs:
        rep = r.report()
        status = "ERROR" if r.error else r.stop_reason.upper()
        lines.append("")
        lines.append(f"## [{r.index + 1}] {r.name} — {status} in {rep['duration_s']}s, {r.rounds} rounds, {r.tool_calls} tool calls ({r.failed_calls} failed), child chat {r.session_id}")
        if r.error:
            lines.append(f"   error: {r.error}")
        if r.mutations:
            lines.append("   files changed: " + ", ".join(r.mutations[:20]))
        else:
            lines.append("   files changed: NONE")
        bad = [c for c in r.static_checks if not c.get("ok")]
        if bad:
            lines.append("   syntax errors: " + "; ".join(f"{c['path']}: {c['error']}" for c in bad[:5]))
        elif r.static_checks:
            lines.append(f"   syntax check passed for {len(r.static_checks)} file(s)")
        if r.rejections:
            lines.append(f"   harness rejections: {r.rejections}")
        if r.text.strip():
            lines.append("   worker report: " + _short(r.text, 900))
    if workspace:
        try:
            from src.agent_harness import git_change_summary
            g = git_change_summary(workspace)
            if g:
                lines.append("")
                lines.append(f"Workspace git status after all workers: {g.get('changed_count', 0)} path(s) changed" + (f" ({g.get('shortstat')})" if g.get('shortstat') else ""))
                for c in (g.get("changed") or [])[:30]:
                    lines.append(f"   {c['status']:<2} {c['path']}")
        except Exception:
            pass
    lines.append("")
    lines.append("Report to the user ONLY what is listed above as evidence. If a worker changed no files, say so plainly.")
    return "\n".join(lines)


class DelegateAgentsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = parse_delegation_args(content)
        except ValueError as e:
            return {"error": str(e), "exit_code": 1}
        parent_sid = ctx.get("session_id")
        owner = ctx.get("owner")
        progress_cb = ctx.get("progress_cb")
        from src.ai_interaction import get_session_manager
        from src.tool_execution import get_active_workspace, get_active_workspace_roots
        sm = get_session_manager()
        parent = sm.get_session(parent_sid) if (sm and parent_sid) else None
        if parent is None:
            return {"error": "delegate_agents: parent chat session not found", "exit_code": 1}
        endpoint_url = str(getattr(parent, "endpoint_url", "") or "")
        model = str(getattr(parent, "model", "") or "")
        headers = getattr(parent, "headers", None) or None
        if not endpoint_url or not model:
            return {"error": "delegate_agents: parent session has no model route", "exit_code": 1}
        workspace = get_active_workspace()
        roots = list(get_active_workspace_roots() or ()) or None
        gen_overrides = ctx.get("gen_overrides") if isinstance(ctx.get("gen_overrides"), dict) else None

        runs = [SubagentRun(i, t) for i, t in enumerate(args["tasks"])]
        for r in runs:
            if r.model_override:
                # Per-task model override is resolved against the same endpoint.
                pass

        async def emit_for(run: SubagentRun):
            async def _emit(payload: Dict[str, Any]):
                if progress_cb is None:
                    return
                try:
                    await progress_cb({"subagent": {"id": run.id, "index": run.index, "name": run.name, **payload}})
                except Exception:
                    pass
            return _emit

        async def one(run: SubagentRun):
            await _run_subagent(
                run,
                endpoint_url=endpoint_url, model=run.model_override or model, headers=headers, owner=owner,
                workspace=workspace, workspace_roots=roots, max_rounds=args["max_rounds"],
                shared_context=args["shared_context"], parent_session_id=parent_sid,
                emit=await emit_for(run), gen_overrides=gen_overrides,
            )

        t0 = time.time()
        if args["parallel"] and len(runs) > 1:
            await asyncio.gather(*(one(r) for r in runs), return_exceptions=True)
        else:
            for r in runs:
                await one(r)
        report = _build_report_text(runs, workspace)
        return {
            "output": report,
            "exit_code": 0 if not any(r.error for r in runs) else 1,
            "subagents": [r.report() for r in runs],
            "duration_s": round(time.time() - t0, 1),
        }
