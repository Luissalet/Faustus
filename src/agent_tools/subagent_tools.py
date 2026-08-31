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
import contextvars
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_SUBAGENTS = 4
DEFAULT_MAX_ROUNDS = 14
DEFAULT_WORKER_TIMEOUT_S = 1500   # wall-clock bound per worker (25 min; qwen3-coder on this GPU does a task in 1-5 min)
SUBAGENT_FOLDER = "Agents"
REVIEWER_NAME = "reviewer"


# ---------------------------------------------------------------------------
# v2: exclusive files per worker (a lock, not a warning)
# ---------------------------------------------------------------------------
#
# Two workers writing the same file is the classic parallel-agent failure.
# A delegation run owns one FileLockRegistry; every worker runs with a
# _LockGuard in its task context (contextvar → inherited by the tool tasks the
# agent loop spawns). Before a write tool runs, src/tool_execution asks
# `write_block_reason()`; a file owned by another worker is refused with an
# explanatory error the model can act on. Ownership comes from the task's
# declared `files` (pre-claimed) or first-writer-wins at the first successful
# write. The optional reviewer runs after the workers and bypasses the locks.

class FileLockRegistry:
    def __init__(self, workspace: Optional[str]):
        self.workspace = os.path.realpath(workspace) if workspace else None
        self.owner: Dict[str, str] = {}      # normalised path → worker name
        self.display: Dict[str, str] = {}    # normalised path → path as first seen
        self.conflicts: List[Dict[str, str]] = []

    def norm(self, path: str) -> Optional[str]:
        if not path:
            return None
        try:
            if self.workspace and not os.path.isabs(path):
                real = os.path.realpath(os.path.join(self.workspace, path))
            else:
                real = os.path.realpath(path)
        except (OSError, ValueError):
            return None
        if self.workspace:
            try:
                rel = os.path.relpath(real, self.workspace)
            except ValueError:
                rel = real
            if not rel.startswith(".."):
                real = rel
        key = real.replace("\\", "/")
        return key.lower() if os.name == "nt" else key

    def claim(self, worker: str, paths: List[str]) -> List[str]:
        """Claim paths for `worker`; returns the ones already owned by someone else."""
        taken: List[str] = []
        for p in paths:
            key = self.norm(p)
            if not key:
                continue
            cur = self.owner.get(key)
            if cur is None:
                self.owner[key] = worker
                self.display[key] = p
            elif cur != worker:
                taken.append(p)
        return taken

    def blocked_by(self, worker: str, paths: List[str]) -> Optional[str]:
        """The other worker that owns one of `paths`, or None."""
        for p in paths:
            key = self.norm(p)
            if key and self.owner.get(key) not in (None, worker):
                return self.owner[key]
        return None

    def owned_by(self, worker: str) -> List[str]:
        return [self.display[k] for k, w in self.owner.items() if w == worker]


class _LockGuard:
    __slots__ = ("registry", "worker", "bypass")

    def __init__(self, registry: FileLockRegistry, worker: str, bypass: bool = False):
        self.registry = registry
        self.worker = worker
        self.bypass = bypass


_LOCK_CTX: contextvars.ContextVar[Optional[_LockGuard]] = contextvars.ContextVar("odysseus_subagent_locks", default=None)
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


def _targets(tool: str, content: Any) -> List[str]:
    try:
        from src.tool_capabilities import _write_targets
        return list(_write_targets(tool, content) or [])
    except Exception:
        return []


def write_block_reason(tool: Any, content: Any) -> Optional[str]:
    """Called by the tool dispatcher before a write runs. Returns the error
    text when the current worker must not touch that file, else None."""
    guard = _LOCK_CTX.get()
    if guard is None or guard.bypass or not isinstance(tool, str) or tool not in _WRITE_TOOLS:
        return None
    targets = _targets(tool, content)
    if not targets:
        return None
    other = guard.registry.blocked_by(guard.worker, targets)
    if not other:
        return None
    mine = guard.registry.owned_by(guard.worker)
    guard.registry.conflicts.append({"worker": guard.worker, "owner": other, "path": targets[0]})
    return (
        f"{tool}: '{targets[0]}' is owned by sub-agent '{other}' in this delegation — another worker "
        "is editing it and two writers would clobber each other. Do NOT modify it. Finish your own "
        "part" + (f" (your files: {', '.join(mine[:8])})" if mine else "") + " and, in your final "
        "report, describe exactly what change that file needs so the coordinator can apply it."
    )


def note_write_result(tool: Any, content: Any, result: Any) -> None:
    """Called after a write tool ran: first successful writer owns the file."""
    guard = _LOCK_CTX.get()
    if guard is None or guard.bypass or not isinstance(tool, str) or tool not in _WRITE_TOOLS:
        return
    if not isinstance(result, dict) or result.get("error") or result.get("blocked"):
        return
    if result.get("exit_code") not in (None, 0):
        return
    guard.registry.claim(guard.worker, _targets(tool, content))


# ---------------------------------------------------------------------------
# v2: stop one worker
# ---------------------------------------------------------------------------

_ACTIVE_WORKERS: Dict[str, asyncio.Task] = {}   # child session id → the worker's task


def stop_worker(child_session_id: str) -> bool:
    task = _ACTIVE_WORKERS.get(child_session_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def active_worker_ids() -> List[str]:
    return [sid for sid, t in _ACTIVE_WORKERS.items() if not t.done()]


_TASK_FILES_RE = re.compile(r"^\s*\[([^\]]+)\]\s*")
_TASK_MODEL_RE = re.compile(r"^\s*\{([^}]+)\}\s*")
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
    tasks: List[Dict[str, Any]] = []
    for i, t in enumerate(tasks_raw[:MAX_SUBAGENTS]):
        files: List[str] = []
        model = ""
        if isinstance(t, str):
            instruction, name = t.strip(), ""
        elif isinstance(t, dict):
            instruction = str(t.get("instruction") or t.get("task") or t.get("content") or "").strip()
            name = str(t.get("name") or t.get("title") or "").strip()
            raw_files = t.get("files") or t.get("owns") or []
            if isinstance(raw_files, str):
                raw_files = [p for p in re.split(r"[,\s]+", raw_files) if p]
            files = [str(p).strip() for p in raw_files if str(p).strip()][:40] if isinstance(raw_files, list) else []
            model = str(t.get("model") or "").strip()
        else:
            continue
        # Inline prefixes (the /agents slash form): "{model} [a.py, b.py] instruction".
        m = _TASK_MODEL_RE.match(instruction)
        if m:
            model = model or m.group(1).strip()
            instruction = instruction[m.end():]
        m = _TASK_FILES_RE.match(instruction)
        if m:
            files = files or [p.strip() for p in m.group(1).split(",") if p.strip()]
            instruction = instruction[m.end():]
        instruction = instruction.strip()
        if not instruction:
            continue
        if not name or _TASK_FILES_RE.match(name) or _TASK_MODEL_RE.match(name):
            name = _short(instruction, 48)
        tasks.append({"name": name[:80], "instruction": instruction[:8000], "model": model[:120], "files": files})
    if not tasks:
        raise ValueError("delegate_agents: no usable tasks (each needs an 'instruction')")
    reviewer = data.get("reviewer", data.get("review"))
    if reviewer is None:
        try:
            from src.settings import get_setting
            reviewer = bool(get_setting("agent_subagent_reviewer", False))
        except Exception:
            reviewer = False
    reviewer_model = str(data.get("reviewer_model") or "").strip()
    if len(tasks_raw) > MAX_SUBAGENTS:
        logger.info("delegate_agents: %s tasks requested, capped at %s", len(tasks_raw), MAX_SUBAGENTS)
    try:
        max_rounds = int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    except (TypeError, ValueError):
        max_rounds = DEFAULT_MAX_ROUNDS
    try:
        timeout_s = int(data.get("timeout_s") or data.get("timeout") or DEFAULT_WORKER_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_WORKER_TIMEOUT_S
    return {
        "tasks": tasks,
        "parallel": bool(data.get("parallel", True)),
        "max_rounds": max(3, min(max_rounds, 40)),
        "shared_context": str(data.get("context") or data.get("shared_context") or "").strip()[:4000],
        "timeout_s": max(60, min(timeout_s, 7200)),
        "reviewer": bool(reviewer),
        "reviewer_model": reviewer_model[:120],
    }


class SubagentRun:
    def __init__(self, index: int, task: Dict[str, Any], role: str = "worker"):
        self.index = index
        self.id = f"sa{index + 1}-{uuid.uuid4().hex[:6]}"
        self.name = task["name"]
        self.instruction = task["instruction"]
        self.model_override = task.get("model") or ""
        self.files: List[str] = list(task.get("files") or [])
        self.role = role
        self.stopped_by_user = False
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
            "role": self.role, "files": self.files, "model": self.model_override or None,
            "instruction": self.instruction[:2000],
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
    locks: Optional[FileLockRegistry] = None,
    harness_options: Optional[Dict[str, Any]] = None,
) -> None:
    from src.agent_loop import stream_agent_loop
    from src.ai_interaction import get_session_manager
    from core.models import ChatMessage

    # Exclusive files: pre-claim the declared ones, then first-writer-wins.
    if locks is not None:
        taken = locks.claim(run.name, run.files) if run.files else []
        if taken:
            logger.info("delegate_agents: %s wanted %s but they belong to another worker", run.name, taken)
        _LOCK_CTX.set(_LockGuard(locks, run.name, bypass=(run.role == "reviewer")))

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

    if run.role == "reviewer":
        preamble = (
            "You are the REVIEWER sub-agent of a delegated job: the other workers have finished. "
            "Your task: review what they changed as a whole — consistency between their parts "
            "(names, signatures, imports, call sites), obvious defects, and anything a worker "
            "reported it could not do because a file belonged to someone else. Read the changed "
            "files, fix real problems with edit_file, run the smallest relevant verification you "
            "can (py_compile, node --check, a focused test). Do not refactor or restyle. Finish "
            "with a factual report: what you verified, what you fixed (files), what is still wrong."
        )
    else:
        preamble = (
            "You are a sub-agent working on ONE delegated task inside a larger job. "
            "Work only on this task, in the shared workspace, using tools. Do not ask "
            "the user questions — decide and act. Finish with a short factual report of "
            "what you changed (files) and what you verified."
        )
        if run.files:
            preamble += (
                "\n\nFILES YOU OWN (exclusive — other workers cannot write them, and you must not write "
                "any file owned by another worker; if another file needs a change, describe it in your "
                "report instead): " + ", ".join(run.files[:40])
            )
        elif locks is not None:
            preamble += (
                "\n\nFile ownership is exclusive per worker: the first worker that writes a file owns it "
                "for this job. If a write is refused because another worker owns the file, do not retry — "
                "describe the needed change in your report."
            )
    if shared_context:
        preamble += "\n\nShared context from the coordinator:\n" + shared_context
    messages = [{"role": "user", "content": f"{preamble}\n\nYOUR TASK: {run.instruction}"}]

    await emit({"event": "started", "name": run.name, "instruction": _short(run.instruction, 240), "session_id": child_sid,
                "role": run.role, "files": run.files, "model": run.model_override or model})
    final_metrics: Dict[str, Any] = {}
    tool_events: List[Dict[str, Any]] = []
    # Sidebar activity: the worker chat blinks while it works, then shows as
    # finished-unread — same as a chat the user started themselves.
    try:
        from src import agent_runs as _agent_runs
        _agent_runs.mark_busy(child_sid)
    except Exception:
        _agent_runs = None
    # Workers do not checkpoint (the coordinator did, before delegating), do
    # not run the project's tests or the reviewer pass (the coordinator's turn
    # does, once, over everything), and do not need the repo map twice.
    _worker_opts = dict(harness_options or {})
    _worker_opts.update({"checkpoints": False, "run_tests": False, "review_model": "off"})
    try:
        async for chunk in stream_agent_loop(
            endpoint_url, model, messages,
            headers=headers, temperature=0.3, max_tokens=0,
            max_rounds=max_rounds, session_id=child_sid, owner=owner,
            workspace=workspace, workspace_roots=workspace_roots,
            disabled_tools=set(SUBAGENT_DISABLED_TOOLS),
            security_gate_bypass=True, _is_teacher_run=True,
            gen_overrides=gen_overrides,
            harness_options=_worker_opts,
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
        if _agent_runs is not None:
            try:
                _agent_runs.clear_busy(child_sid)
            except Exception:
                pass
        # Persist the transcript into the child chat so it can be audited later
        # (also when the worker was cancelled/timed out: what it did is evidence).
        if sm and run.session_id:
            try:
                child = sm.get_session(run.session_id)
                if child is not None:
                    child.add_message(ChatMessage("user", run.instruction))
                    meta = dict(final_metrics or {})
                    meta["tool_events"] = tool_events[:60]
                    meta["subagent"] = {"parent_session": parent_session_id, "name": run.name}
                    if run.error:
                        meta["subagent"]["error"] = run.error
                    child.add_message(ChatMessage("assistant", run.text.strip() or "(no final text)", metadata=meta))
                    sm.save_sessions()
            except Exception as e:
                logger.debug("delegate_agents: transcript save failed: %s", e)
    await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300)})


def _build_report_text(runs: List[SubagentRun], workspace: Optional[str], locks: Optional[FileLockRegistry] = None) -> str:
    workers = [r for r in runs if r.role != "reviewer"]
    lines = [f"Delegated {len(workers)} sub-agent task(s). Evidence-based report (from tool logs, not from the workers' prose):"]
    for r in runs:
        rep = r.report()
        status = "ERROR" if r.error else r.stop_reason.upper()
        lines.append("")
        tag = "REVIEWER" if r.role == "reviewer" else f"[{r.index + 1}]"
        lines.append(f"## {tag} {r.name} — {status} in {rep['duration_s']}s, {r.rounds} rounds, {r.tool_calls} tool calls ({r.failed_calls} failed), child chat {r.session_id}")
        if r.error:
            lines.append(f"   error: {r.error}")
        if r.files:
            lines.append("   owned files: " + ", ".join(r.files[:20]))
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
    # Two workers writing the same file is the classic parallel-agent failure:
    # the later write may have clobbered the earlier one. Call it out.
    seen: Dict[str, List[str]] = {}
    for r in runs:
        for p in r.mutations:
            key = str(p).replace("\\", "/").lower()
            seen.setdefault(key, [])
            if r.name not in seen[key]:
                seen[key].append(r.name)
    overlaps = {k: v for k, v in seen.items() if len(v) > 1 and not (locks is not None and REVIEWER_NAME in [n.lower() for n in v])}
    if overlaps:
        lines.append("")
        lines.append("WARNING — files changed by MORE THAN ONE worker (review for clobbered edits):")
        for k, names in list(overlaps.items())[:10]:
            lines.append(f"   {k}: " + ", ".join(names))
    if locks is not None and locks.conflicts:
        lines.append("")
        lines.append("File-lock refusals (a worker tried to write a file owned by another; the write was blocked, "
                     "the worker was told to describe the change instead):")
        for c in locks.conflicts[:10]:
            lines.append(f"   {c['worker']} → {c['path']} (owned by {c['owner']})")
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
        locks = FileLockRegistry(workspace)
        harness_options = ctx.get("harness_options") if isinstance(ctx.get("harness_options"), dict) else None

        async def emit_for(run: SubagentRun):
            async def _emit(payload: Dict[str, Any]):
                if progress_cb is None:
                    return
                try:
                    await progress_cb({"subagent": {"id": run.id, "index": run.index, "name": run.name, "role": run.role, **payload}})
                except Exception:
                    pass
            return _emit

        async def one(run: SubagentRun, max_rounds: Optional[int] = None):
            emit = await emit_for(run)
            try:
                # Wall-clock bound per worker: a worker stuck on a foreground
                # server or a silent model must not hang the coordinator.
                await asyncio.wait_for(
                    _run_subagent(
                        run,
                        endpoint_url=endpoint_url, model=run.model_override or model, headers=headers, owner=owner,
                        workspace=workspace, workspace_roots=roots, max_rounds=max_rounds or args["max_rounds"],
                        shared_context=args["shared_context"], parent_session_id=parent_sid,
                        emit=emit, gen_overrides=gen_overrides, locks=locks, harness_options=harness_options,
                    ),
                    timeout=args["timeout_s"],
                )
            except asyncio.TimeoutError:
                run.error = run.error or f"worker timed out after {args['timeout_s']}s (its running command was killed)"
                run.stop_reason = "timeout"
                run.finished = run.finished or time.time()
                await emit({"event": "error", "message": run.error})
                await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300)})
            except asyncio.CancelledError:
                # Stopped from the UI (stop_worker): the transcript was saved by
                # _run_subagent's finally; report it as stopped, not as a crash.
                run.stopped_by_user = True
                run.error = None
                run.stop_reason = "stopped"
                run.finished = run.finished or time.time()
                await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300) or "(stopped by the user)"})
            finally:
                if run.session_id:
                    _ACTIVE_WORKERS.pop(run.session_id, None)

        def _launch(run: SubagentRun, max_rounds: Optional[int] = None) -> asyncio.Task:
            task = asyncio.create_task(one(run, max_rounds))
            # The child session id is assigned inside _run_subagent; register
            # the task under it as soon as it exists so the UI can stop it.
            async def _register():
                for _ in range(200):
                    if run.session_id:
                        if not task.done():
                            _ACTIVE_WORKERS[run.session_id] = task
                        return
                    await asyncio.sleep(0.05)
            asyncio.create_task(_register())
            return task

        t0 = time.time()
        if args["parallel"] and len(runs) > 1:
            await asyncio.gather(*(_launch(r) for r in runs), return_exceptions=True)
        else:
            for r in runs:
                try:
                    await _launch(r)
                except Exception:
                    pass
        # Optional reviewer: one more worker over the whole result, after the
        # others, with lock bypass (nobody else writes any more).
        if args.get("reviewer") and any(r.mutations for r in runs):
            changed: Dict[str, List[str]] = {}
            for r in runs:
                for p in r.mutations:
                    changed.setdefault(p, []).append(r.name)
            summary_lines = [f"- {p} (by {', '.join(names)})" for p, names in list(changed.items())[:40]]
            reports = [f"[{r.name}] {_short(r.text, 600)}" for r in runs if r.text.strip()]
            instruction = (
                "Files changed by the workers:\n" + "\n".join(summary_lines)
                + ("\n\nWorker reports:\n" + "\n".join(reports) if reports else "")
                + ("\n\nWrites refused by the file locks (the change may still be needed): "
                   + "; ".join(f"{c['worker']} → {c['path']}" for c in locks.conflicts[:10]) if locks.conflicts else "")
            )
            reviewer = SubagentRun(len(runs), {"name": REVIEWER_NAME, "instruction": instruction,
                                               "model": args.get("reviewer_model") or "", "files": []}, role="reviewer")
            try:
                await _launch(reviewer, max_rounds=max(6, min(args["max_rounds"], 16)))
            except Exception:
                pass
            runs.append(reviewer)
        report = _build_report_text(runs, workspace, locks)
        return {
            "output": report,
            "exit_code": 0 if not any(r.error for r in runs) else 1,
            "subagents": [r.report() for r in runs],
            "duration_s": round(time.time() - t0, 1),
            "lock_conflicts": list(locks.conflicts),
        }
