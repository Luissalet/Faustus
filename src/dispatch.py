"""Dispatch: local workers for an outside coordinator (Fable / Claude / any
API caller), so the expensive model plans and reviews and never reads a tool
transcript.

    job = start(owner, {"tasks": [...], "workspace": "D:/proj", ...})
    ...
    compact(job)  →  a few hundred tokens: per task status, what changed,
                     tests, the worker's last words; never the transcript.

Each job runs the SAME machinery as `/agents` in a chat (delegate_agents:
src/agent_tools/subagent_tools.py — file locks, watchdog, supervisor, GPU
semaphore, lean toolset, the control board), inside a "Workers" chat of its
own so the human can open the board, steer or stop a worker, and read the
transcripts. The model is `dispatch_endpoint_id` / `dispatch_model` from
settings (falls back to the utility, then the default chat model), or the
request's `model` on that endpoint; the request never names a URL.

Jobs live in memory with a JSON mirror under DATA_DIR/dispatch/ so a
finished job can still be read after a restart (a running one is reported as
`interrupted`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_TASKS = 4                 # delegate_agents' own cap (MAX_SUBAGENTS)
MAX_JOBS_KEPT = 200
SUMMARY_CHARS = 1200          # the worker's last words, per task
EVENTS_KEPT = 400
_DEFAULT_TIMEOUT_S = 900
_DEFAULT_MAX_ROUNDS = 20

_jobs: Dict[str, "DispatchJob"] = {}
_lock = asyncio.Lock()


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return os.path.join(DATA_DIR, "dispatch")
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data", "dispatch")


class DispatchJob:
    def __init__(self, owner: Optional[str], args: Dict[str, Any], workspace: Optional[str],
                 endpoint_url: str, model: str, headers: Optional[Dict[str, str]],
                 title: str, gen_overrides: Optional[Dict[str, Any]] = None):
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner
        self.args = args
        self.workspace = workspace
        self.endpoint_url = endpoint_url
        self.model = model
        self.headers = headers
        self.title = title
        self.gen_overrides = gen_overrides
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.status = "queued"            # queued | running | done | error | cancelled | interrupted
        self.error: Optional[str] = None
        self.session_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.events: Deque[Dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        self.task: Optional[asyncio.Task] = None
        self._waiters: List[asyncio.Event] = []

    # ── views ────────────────────────────────────────────────────────────

    def to_dict(self, *, include_result: bool = True) -> Dict[str, Any]:
        d = {
            "id": self.id, "owner": self.owner, "title": self.title, "status": self.status,
            "error": self.error, "workspace": self.workspace, "model": self.model,
            "session_id": self.session_id, "chat_url": f"/#{self.session_id}" if self.session_id else None,
            "created": self.created, "started": self.started, "finished": self.finished,
            "duration_s": round((self.finished or time.time()) - (self.started or self.created), 1),
            "tasks": [{"name": t.get("name"), "instruction": t.get("instruction"), "files": t.get("files") or [],
                       "model": t.get("model") or None} for t in self.args.get("tasks") or []],
            "parallel": bool(self.args.get("parallel")), "reviewer": bool(self.args.get("reviewer")),
            "max_rounds": self.args.get("max_rounds"), "timeout_s": self.args.get("timeout_s"),
        }
        if include_result:
            d["result"] = self.result
        return d

    def _notify(self) -> None:
        for ev in self._waiters:
            ev.set()
        self._waiters.clear()

    def _persist(self) -> None:
        try:
            d = _data_dir()
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{self.id}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(d, f"{self.id}.json"))
        except Exception as e:  # noqa: BLE001 — a mirror, never load-bearing
            logger.debug("dispatch: persist failed: %s", e)


# ── the compact answer ──────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _squash(text: Any, limit: int) -> str:
    s = _WS_RE.sub(" ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def compact_from_result(result: Optional[Dict[str, Any]], *, summary_chars: int = SUMMARY_CHARS) -> Dict[str, Any]:
    """What an outside coordinator needs and nothing more: per worker the
    status, the files it changed, its tool/round/token counts, static-check
    and git facts, and its last words — never the transcript."""
    out: Dict[str, Any] = {"workers": [], "files_changed": [], "totals": {
        "tool_calls": 0, "failed_calls": 0, "rounds": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}}
    if not isinstance(result, dict):
        return out
    changed: List[str] = []
    for r in result.get("subagents") or []:
        if not isinstance(r, dict):
            continue
        w = {
            "name": r.get("name"), "role": r.get("role") or "worker", "status": r.get("status"),
            "stop_reason": r.get("stop_reason"), "error": r.get("error"),
            "rounds": int(r.get("rounds") or 0), "tool_calls": int(r.get("tool_calls") or 0),
            "failed_calls": int(r.get("failed_calls") or 0),
            "files_changed": list(r.get("mutations") or []),
            "input_tokens": int(r.get("input_tokens") or 0), "output_tokens": int(r.get("output_tokens") or 0),
            "duration_s": r.get("duration_s"), "model": r.get("model"),
            "summary": _squash(r.get("final_text"), summary_chars),
            "session_id": r.get("session_id"),
        }
        sc = r.get("static_checks")
        if sc:
            w["static_checks"] = sc if isinstance(sc, (dict, list)) else str(sc)[:300]
        git = r.get("git")
        if git:
            w["git"] = git if isinstance(git, (dict, list)) else str(git)[:300]
        if r.get("supervisor"):
            w["supervisor"] = [str(x)[:160] for x in list(r.get("supervisor") or [])[:4]]
        out["workers"].append(w)
        for p in w["files_changed"]:
            if p not in changed:
                changed.append(p)
        t = out["totals"]
        t["tool_calls"] += w["tool_calls"]
        t["failed_calls"] += w["failed_calls"]
        t["rounds"] += w["rounds"]
        t["input_tokens"] += w["input_tokens"]
        t["output_tokens"] += w["output_tokens"]
        if w["error"] or w["status"] == "error":
            t["errors"] += 1
    out["files_changed"] = changed
    if result.get("lock_conflicts"):
        out["lock_conflicts"] = [f"{c.get('worker')} → {c.get('path')}" for c in list(result["lock_conflicts"])[:10]
                                 if isinstance(c, dict)]
    if result.get("dropped_tasks"):
        out["dropped_tasks"] = int(result["dropped_tasks"])
    out["exit_code"] = result.get("exit_code")
    return out


def compact(job: DispatchJob) -> Dict[str, Any]:
    d = job.to_dict(include_result=False)
    d["result"] = compact_from_result(job.result)
    # a running job: the board's latest tick per worker, so a poller sees
    # progress without the event stream
    if job.status in ("queued", "running"):
        latest: Dict[str, Dict[str, Any]] = {}
        for ev in job.events:
            name = str(ev.get("name") or ev.get("id") or "")
            if not name:
                continue
            kind = ev.get("event")
            if kind in ("started", "round", "tool", "tick", "steer", "supervisor", "done", "error"):
                cur = latest.setdefault(name, {})
                cur["last_event"] = kind
                for k in ("round", "elapsed_s", "idle_s", "last_tool", "tool", "stalled", "stall_reason", "status"):
                    if k in ev:
                        cur[k] = ev[k]
        d["progress"] = latest
    return d


# ── running a job ───────────────────────────────────────────────────────────

def _parse_tasks(raw: Any) -> List[Dict[str, Any]]:
    from src.agent_tools.subagent_tools import parse_delegation_args
    args = parse_delegation_args(json.dumps({"tasks": raw}) if not isinstance(raw, str) else raw)
    return list(args.get("tasks") or [])


def build_args(body: Dict[str, Any]) -> Dict[str, Any]:
    """The delegate_agents payload for a dispatch request (validated by the
    tool's own parser so a job and a chat delegation cannot disagree)."""
    from src.agent_tools.subagent_tools import parse_delegation_args
    payload: Dict[str, Any] = {"tasks": body.get("tasks")}
    for k in ("parallel", "reviewer", "max_rounds", "timeout_s", "reviewer_model"):
        if body.get(k) is not None:
            payload[k] = body[k]
    if body.get("context"):
        payload["context"] = str(body["context"])[:8000]
    args = parse_delegation_args(json.dumps(payload))
    if not args.get("tasks"):
        raise ValueError("tasks is required: a list of instructions or {instruction, files?, model?, name?}")
    if not body.get("max_rounds"):
        args["max_rounds"] = _DEFAULT_MAX_ROUNDS
    if not body.get("timeout_s"):
        args["timeout_s"] = _DEFAULT_TIMEOUT_S
    return args


def resolve_route(owner: Optional[str], model: Optional[str] = None) -> tuple[str, str, Optional[Dict[str, str]]]:
    """(endpoint_url, model, headers) for the workers: the dispatch endpoint
    from settings (→ utility → default chat model); `model` picks another
    model ON that endpoint (never another server)."""
    from src.endpoint_resolver import resolve_endpoint
    url, resolved_model, headers = resolve_endpoint("dispatch", owner=owner)
    if not url:
        raise ValueError("no model endpoint is configured for dispatch (Settings → Agent & automation → Fable workers, or a default chat model)")
    # resolve_endpoint only honours `dispatch_model` together with
    # `dispatch_endpoint_id`; a model chosen without an endpoint id (the
    # common case: "the workers use qwen3.5:9b on the usual server") must
    # still win over the utility / default chat model — seen live: the
    # 29 GB q8_0 default model picked up a dispatched job.
    configured = ""
    try:
        from src.settings import get_setting
        configured = str(get_setting("dispatch_model", "") or "").strip()
    except Exception:
        configured = ""
    m = str(model or "").strip() or configured or str(resolved_model or "")
    if not m:
        raise ValueError("no model configured for dispatch")
    return url, m, headers


def _title(args: Dict[str, Any]) -> str:
    tasks = args.get("tasks") or []
    first = _squash((tasks[0] or {}).get("instruction"), 60) if tasks else "workers"
    return f"Workers · {first}" if len(tasks) == 1 else f"Workers ({len(tasks)}) · {first}"


async def _run(job: DispatchJob) -> None:
    from src.agent_tools.subagent_tools import DelegateAgentsTool
    from src import tool_execution as te
    job.started = time.time()
    job.status = "running"
    job._persist()

    async def _cb(payload: Dict[str, Any]) -> None:
        ev = payload.get("subagent") if isinstance(payload, dict) else None
        if isinstance(ev, dict):
            job.events.append(dict(ev))

    token = te._active_workspace.set(job.workspace or None)
    roots_token = te._active_workspace_roots.set((job.workspace,) if job.workspace else ())
    try:
        tool = DelegateAgentsTool()
        ctx = {"session_id": job.session_id, "owner": job.owner, "progress_cb": _cb,
               "gen_overrides": job.gen_overrides or None}
        result = await tool.execute(json.dumps(job.args), ctx)
        job.result = result if isinstance(result, dict) else {"output": str(result)}
        if job.result.get("error") and not job.result.get("subagents"):
            job.status = "error"
            job.error = str(job.result.get("error"))[:500]
        else:
            job.status = "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch %s failed", job.id)
        job.status = "error"
        job.error = str(e)[:500]
    finally:
        te._active_workspace.reset(token)
        te._active_workspace_roots.reset(roots_token)
        job.finished = time.time()
        _record_turn(job)
        job._persist()
        job._notify()


def _record_turn(job: DispatchJob) -> None:
    """Write the job into its Workers chat the way a chat turn would: one
    assistant message whose tool_event carries the delegate_agents evidence,
    so the control board is rebuilt from history when the chat is opened
    (the same `subagents` shape src/agent_loop.py persists)."""
    if not job.session_id:
        return
    try:
        from core.models import ChatMessage
        from src.ai_interaction import get_session_manager
        from src.agent_loop import _compact_subagent_reports
        sm = get_session_manager()
        result = job.result if isinstance(job.result, dict) else {}
        reports = result.get("subagents") if isinstance(result.get("subagents"), list) else []
        comp = compact_from_result(result)
        lines = [f"Dispatched job {job.id}: {job.status}" + (f" — {job.error}" if job.error else "")]
        for w in comp.get("workers") or []:
            lines.append(f"- {w.get('name')}: {w.get('status')}"
                         + (f" — changed {', '.join(w.get('files_changed') or [])}" if w.get("files_changed") else ""))
        ev = {
            "round": 1, "model": job.model, "tool": "delegate_agents",
            "desc": f"{len(job.args.get('tasks') or [])} worker(s) dispatched from outside Faustus",
            "command": json.dumps({"tasks": [t.get("instruction", "")[:300] for t in job.args.get("tasks") or []],
                                   "parallel": bool(job.args.get("parallel"))}, ensure_ascii=False),
            "output": str(result.get("output") or job.error or job.status)[:4000],
            "exit_code": 0 if job.status == "done" and not result.get("exit_code") else 1,
            "subagents": _compact_subagent_reports(reports) if reports else [],
            "dispatch_id": job.id,
        }
        sm.add_message(job.session_id, ChatMessage("assistant", "\n".join(lines),
                                                   metadata={"tool_events": [ev], "model": job.model,
                                                             "source": "dispatch", "dispatch_id": job.id}))
        try:
            sm.save_sessions()
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001 — the board is a courtesy; the job's answer is the compact result
        logger.debug("dispatch %s: could not record the turn: %s", job.id, e)


def _make_session(job: DispatchJob) -> str:
    """The parent chat every dispatched job runs in: the board lives there,
    the transcripts land there, the human can steer or stop from there."""
    from src.ai_interaction import get_session_manager
    sm = get_session_manager()
    sid = str(uuid.uuid4())
    session = sm.create_session(session_id=sid, name=job.title, endpoint_url=job.endpoint_url,
                                model=job.model, rag=False, owner=job.owner)
    if job.headers:
        try:
            session.headers = dict(job.headers)
        except Exception:
            pass
    try:
        from core.models import ChatMessage
        sm.add_message(sid, ChatMessage(role="user", content=_dispatch_note(job),
                                        metadata={"source": "dispatch", "dispatch_id": job.id}))
    except Exception:
        pass
    return sid


def _dispatch_note(job: DispatchJob) -> str:
    lines = [f"Dispatched from outside Faustus (job {job.id}) — {len(job.args.get('tasks') or [])} task(s), "
             f"workspace: {job.workspace or '—'}, model: {job.model}."]
    for i, t in enumerate(job.args.get("tasks") or [], 1):
        lines.append(f"{i}. {_squash(t.get('instruction'), 300)}")
    return "\n".join(lines)


async def start(owner: Optional[str], body: Dict[str, Any], *, runner: Optional[Callable] = None) -> DispatchJob:
    """Validate, create the Workers chat and launch the job in the background."""
    args = build_args(body)
    workspace = None
    raw_ws = str(body.get("workspace") or "").strip()
    if raw_ws:
        from src.tool_execution import vet_workspace
        workspace = vet_workspace(raw_ws)
        if not workspace:
            raise ValueError(f"workspace is not a usable directory: {raw_ws}")
    url, model, headers = resolve_route(owner, body.get("model"))
    gen = body.get("gen_overrides") if isinstance(body.get("gen_overrides"), dict) else None
    job = DispatchJob(owner, args, workspace, url, model, headers, _title(args), gen)
    job.session_id = _make_session(job)
    async with _lock:
        _jobs[job.id] = job
        if len(_jobs) > MAX_JOBS_KEPT:
            for old in sorted(_jobs.values(), key=lambda j: j.created)[: len(_jobs) - MAX_JOBS_KEPT]:
                if old.status not in ("queued", "running"):
                    _jobs.pop(old.id, None)
    job._persist()
    job.task = asyncio.create_task((runner or _run)(job))
    return job


def get(job_id: str) -> Optional[DispatchJob]:
    job = _jobs.get(str(job_id or ""))
    if job is not None:
        return job
    return _load(job_id)


def _load(job_id: str) -> Optional[DispatchJob]:
    if not re.fullmatch(r"[0-9a-f]{12}", str(job_id or "")):
        return None
    path = os.path.join(_data_dir(), f"{job_id}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    job = DispatchJob(d.get("owner"), {"tasks": d.get("tasks") or [], "parallel": d.get("parallel"),
                                       "reviewer": d.get("reviewer"), "max_rounds": d.get("max_rounds"),
                                       "timeout_s": d.get("timeout_s")},
                      d.get("workspace"), "", d.get("model") or "", None, d.get("title") or "Workers")
    job.id = d.get("id") or job_id
    job.created = float(d.get("created") or 0)
    job.started = d.get("started")
    job.finished = d.get("finished")
    job.session_id = d.get("session_id")
    job.error = d.get("error")
    job.result = d.get("result")
    # a job that was running when the server stopped never finished
    job.status = "interrupted" if d.get("status") in ("queued", "running") else (d.get("status") or "done")
    _jobs[job.id] = job
    return job


def list_jobs(owner: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    _load_all()
    rows = [j for j in _jobs.values() if owner is None or j.owner == owner]
    rows.sort(key=lambda j: j.created, reverse=True)
    return [j.to_dict(include_result=False) for j in rows[:limit]]


def _load_all() -> None:
    try:
        names = sorted(os.listdir(_data_dir()))
    except OSError:
        return
    for name in names:
        if name.endswith(".json") and name[:-5] not in _jobs:
            _load(name[:-5])


async def wait(job: DispatchJob, timeout: float) -> bool:
    """True when the job is finished (possibly already)."""
    if job.status not in ("queued", "running"):
        return True
    ev = asyncio.Event()
    job._waiters.append(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.0, timeout))
        return True
    except asyncio.TimeoutError:
        return job.status not in ("queued", "running")
    finally:
        if ev in job._waiters:
            job._waiters.remove(ev)


def cancel(job: DispatchJob) -> bool:
    if job.task is not None and not job.task.done():
        job.task.cancel()
        job.status = "cancelled"
        job.finished = time.time()
        job._persist()
        job._notify()
        return True
    return False


COORDINATOR_GUIDE = """\
# Using Faustus workers (for the coordinating model)

You are the planner and the reviewer. The workers are local models on the
user's machine: cheap, tireless, good at mechanical steps, weaker at judgement.
Your own tokens are the scarce resource — spend them on deciding WHAT to do
and on checking the result, not on reading files or running tests yourself.

## When to dispatch
- Editing or creating files, running tests/linters/builds, fixing what fails,
  refactors with a clear spec, searching a codebase, converting formats,
  writing boilerplate or docs from a spec: dispatch.
- Deciding the design, judging trade-offs, anything ambiguous, anything the
  user must decide, the final answer to the user: keep.

## How to write a task (each task = one worker)
1. Self-contained: name the files, the function/class, the behaviour, and the
   exact command that proves it ("`pytest -q` in the workspace must pass").
   The worker does not see this conversation — everything it needs goes in
   the instruction or in `context`.
2. One outcome per task. Two changes that touch the same file go in ONE task
   (parallel workers lock files against each other).
3. Say what NOT to do when it matters ("do not touch the public API",
   "keep Python 3.11 compatibility").
4. 1–4 tasks per job. Independent tasks → `parallel: true`; dependent ones →
   `parallel: false` (they run in order) or separate jobs.
5. `workspace` is the folder the workers are confined to. Always set it.

## Reading the result
`workers_wait` returns, per worker: status (`done`, `error`, `timeout`,
`stalled`, `stopped`), files changed, static checks, git state, tool/round
counts, and the worker's last words (≤ 1200 chars). It never returns the
transcript. Trust files changed + tests over the worker's prose: if the
summary claims a change but `files_changed` is empty, it did not happen.
A worker that ended `stalled` or `timeout` did part of the work — look at
`files_changed`, then dispatch the remainder as a new, narrower task.

## Loop
plan → dispatch → wait → check → (dispatch fixes) → answer the user.
Do not re-do a worker's work yourself; send a narrower task instead. Tell the
user which changes came from the workers and point them at the board
(`chat_url`) if they want the details.
"""


def reset_for_tests() -> None:
    _jobs.clear()
