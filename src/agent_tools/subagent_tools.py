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
MIN_WORKER_TIMEOUT_S = 60         # floor for the per-worker timeout (counted from `started`, not from queueing)
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
        # Owners are keyed by the run's ID (`sa{i}-{hex}`), never by its name:
        # the name comes from the MODEL, and two tasks that happened to share a
        # name were one owner — so they never blocked each other.
        self.owner: Dict[str, str] = {}      # normalised path → worker key
        self.display: Dict[str, str] = {}    # normalised path → path as first seen
        self.names: Dict[str, str] = {}      # worker key → human label (run.name)
        self.conflicts: List[Dict[str, str]] = []
        # Paths claimed at CHECK time by `write_block_reason` (the write has not
        # run yet). They become permanent when the write succeeds and are
        # released when it fails, so a failed write does not fence a file off.
        self.provisional: set = set()

    def label(self, worker: str) -> str:
        """The name to SHOW for a worker key (the key itself if unregistered)."""
        return self.names.get(worker, worker)

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

    def claim(self, worker: str, paths: List[str], provisional: bool = False) -> List[str]:
        """Claim paths for `worker`; returns the ones already owned by someone else.

        `provisional=True` marks a claim made before the write ran (see
        `write_block_reason`): `settle()` keeps or releases it afterwards."""
        taken: List[str] = []
        for p in paths:
            key = self.norm(p)
            if not key:
                continue
            cur = self.owner.get(key)
            if cur is None:
                self.owner[key] = worker
                self.display[key] = p
                if provisional:
                    self.provisional.add(key)
            elif cur != worker:
                taken.append(p)
            elif not provisional:
                self.provisional.discard(key)
        return taken

    def settle(self, worker: str, paths: List[str], ok: bool) -> None:
        """The write behind a provisional claim finished: keep the file on
        success, give it back on failure. Declared (non-provisional) files are
        never released here."""
        for p in paths:
            key = self.norm(p)
            if not key or key not in self.provisional or self.owner.get(key) != worker:
                continue
            self.provisional.discard(key)
            if not ok:
                self.owner.pop(key, None)
                self.display.pop(key, None)

    def blocked_by(self, worker: str, paths: List[str]) -> Optional[str]:
        """The other worker that owns one of `paths`, or None."""
        for p in paths:
            key = self.norm(p)
            if key and self.owner.get(key) not in (None, worker):
                return self.owner[key]
        return None

    def release(self, worker: str) -> List[str]:
        """Give back every file `worker` owns — called when the worker has
        FINISHED, so a later worker (a dependent task in a sequential run, a
        fixer after verification, the next job in the same folder) may edit
        what it wrote. While it ran nobody else could; after it ran there is
        no second writer to clobber. Returns the released display paths."""
        mine = [k for k, w in self.owner.items() if w == worker]
        out = [self.display.get(k, k) for k in mine]
        for k in mine:
            self.owner.pop(k, None)
            self.display.pop(k, None)
            self.provisional.discard(k)
        return out

    def owned_by(self, worker: str) -> List[str]:
        return [self.display[k] for k, w in self.owner.items() if w == worker]

    def owned_by_others(self, worker: str) -> List[str]:
        """Paths reserved by ANY worker other than `worker`."""
        return [self.display[k] for k, w in self.owner.items() if w != worker]


class _LockGuard:
    __slots__ = ("registry", "worker", "bypass")

    def __init__(self, registry: FileLockRegistry, worker: str, bypass: bool = False):
        self.registry = registry
        self.worker = worker
        self.bypass = bypass


_LOCK_CTX: contextvars.ContextVar[Optional[_LockGuard]] = contextvars.ContextVar("odysseus_subagent_locks", default=None)
#: The running worker's derived permissions (src/subagent_permissions.py), set
#: alongside the lock guard in `_run_subagent` and inherited by every tool task
#: the agent loop spawns. None outside a definition-driven worker, which is
#: what keeps every existing delegation on exactly its old path.
_PERMS_CTX: contextvars.ContextVar[Any] = contextvars.ContextVar("odysseus_subagent_perms", default=None)
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
#: Tools whose first argument is ONE path this module can read out with
#: certainty. `grep`/`glob`/`ls` take a root plus a pattern and answer about a
#: tree, so a `read` rule is not applied to them: refusing a listing on a path
#: rule would be theatre, and allowing one while claiming the rule held would
#: be worse.
_READ_TOOLS = frozenset({"read_file"})


def _targets(tool: str, content: Any) -> Optional[List[str]]:
    """Paths a write tool will touch, or **None** when they cannot be determined.

    `tool_capabilities._write_targets` returns None with fail-CLOSED semantics
    ("an undeterminable target is not inside anything") — typically an
    `apply_patch` that DELETES a file, or arguments that did not parse.
    Flattening that None into `[]` here inverted it: `[]` means "nothing to
    block" and the write went through, so worker B could delete the very file
    worker A had reserved. Keep the two apart.
    """
    try:
        from src.tool_capabilities import _write_targets
        targets = _write_targets(tool, content)
    except Exception:
        return None
    if targets is None:
        return None
    return [str(t) for t in targets if t]


def _read_target(tool: str, content: Any) -> Optional[str]:
    """The single path `read_file` was asked for, or None when it cannot be
    read out of the arguments."""
    if tool not in _READ_TOOLS:
        return None
    if isinstance(content, dict):
        path = content.get("path")
        return str(path).strip() if isinstance(path, str) and path.strip() else None
    raw = str(content or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return None
        path = data.get("path") if isinstance(data, dict) else None
        return str(path).strip() if isinstance(path, str) and path.strip() else None
    first = raw.split("\n", 1)[0].strip()
    return first or None


def permission_block_reason(tool: Any, content: Any) -> Optional[str]:
    """The agent definition's own answer to "may this worker run this call?".

    Checked BEFORE the file locks and, unlike them, never bypassed: the
    reviewer's lock bypass exists because nobody else is writing while it runs,
    which says nothing about what its own definition allows it to do. A
    read-only reviewer that could write as soon as it was the reviewer would be
    the exact failure this file is supposed to make impossible.

    An undeterminable target fails CLOSED, and only while the definition
    actually restricts that action — the same shape the lock guard uses, for
    the same reason: a write whose target is unknown cannot be checked against
    a pattern, and a rule that can be walked past by writing an unparseable
    call is not a rule.
    """
    perms = _PERMS_CTX.get()
    if perms is None or not isinstance(tool, str) or not tool:
        return None
    if perms.tool_denied(tool):
        return (f"{tool}: refused — {perms.why_tool_denied(tool)} (agent definition). Do not call it "
                f"again in this turn; do the part of the task your tools reach and say in your "
                f"report what you could not do and why.")
    action = "write" if tool in _WRITE_TOOLS else ("read" if tool in _READ_TOOLS else "")
    if not action or not perms.restricts_action(action):
        return None
    workspace = getattr(perms, "workspace", "") or None
    if action == "write":
        targets = _targets(tool, content)
        if targets is None:
            return (f"{tool}: refused — this call's target file(s) could not be determined (a patch "
                    f"that deletes a file, or arguments this server could not parse) and agent "
                    f"`{perms.slug or 'this worker'}` has path rules that must be checked against a "
                    f"path. Re-issue it as a write_file/edit_file naming ONE path.")
    else:
        one = _read_target(tool, content)
        targets = [one] if one else None
        if targets is None:
            return None      # nothing to check; `read_file` with no path fails on its own
    from src.subagent_permissions import normalise_path
    for raw in targets or ():
        path = normalise_path(raw, workspace)
        if perms.path_denied(action, path):
            return (f"{tool}: refused — {perms.why_path_denied(action, path)} and `{path}` matches it. "
                    f"This is the agent definition this worker was started with, not a lock another "
                    f"worker holds: retrying will not help. Report what that file needs instead.")
    return None


def write_block_reason(tool: Any, content: Any) -> Optional[str]:
    """Called by the tool dispatcher before a write runs. Returns the error
    text when the current worker must not touch that file, else None.

    Two gates, in this order: what the worker's own agent definition allows,
    then what another worker in the same delegation already owns. The first is
    about authority and the second about collision, and only the second is
    ever bypassed.
    """
    denied = permission_block_reason(tool, content)
    if denied:
        return denied
    guard = _LOCK_CTX.get()
    if guard is None or guard.bypass or not isinstance(tool, str) or tool not in _WRITE_TOOLS:
        return None
    reg = guard.registry
    targets = _targets(tool, content)
    if targets is None:
        # Undeterminable target. Refuse only while another worker actually has
        # something to lose — a lone worker (or the first to move) is free.
        others = reg.owned_by_others(guard.worker)
        if not others:
            return None
        reg.conflicts.append({"worker": reg.label(guard.worker), "owner": "(several)",
                              "path": f"{tool}: undetermined target"})
        logger.warning("delegate_agents: refused %s from %s — target not determinable while %s are locked",
                       tool, reg.label(guard.worker), others[:8])
        return (
            f"{tool}: this call's target file(s) could not be determined (a patch that deletes a file, "
            "or arguments this server could not parse) and other sub-agents in this delegation have "
            f"reserved files ({', '.join(others[:8])}). Refused: a write whose target is unknown could "
            "clobber or delete a file another worker owns. Re-issue the change as an explicit "
            "write_file/edit_file naming ONE path you own, and describe any change another worker's "
            "file needs in your final report instead."
        )
    if not targets:
        return None
    other = reg.blocked_by(guard.worker, targets)
    if not other:
        # Claim NOW, not after the write ran: the check happened before
        # dispatch and the claim after execution, so two parallel workers
        # checking the same unowned file both passed and both wrote it.
        # `note_write_result` settles the claim (keeps it, or releases it
        # when the write failed).
        reg.claim(guard.worker, targets, provisional=True)
        return None
    mine = reg.owned_by(guard.worker)
    reg.conflicts.append({"worker": reg.label(guard.worker), "owner": reg.label(other), "path": targets[0]})
    return (
        f"{tool}: '{targets[0]}' is owned by sub-agent '{reg.label(other)}' in this delegation — another "
        "worker is editing it and two writers would clobber each other. Do NOT modify it. Finish your own "
        "part" + (f" (your files: {', '.join(mine[:8])})" if mine else "") + " and, in your final "
        "report, describe exactly what change that file needs so the coordinator can apply it."
    )


def note_write_result(tool: Any, content: Any, result: Any) -> None:
    """Called after a write tool ran: first successful writer owns the file.
    A failed write releases the claim `write_block_reason` made for it."""
    guard = _LOCK_CTX.get()
    if guard is None or guard.bypass or not isinstance(tool, str) or tool not in _WRITE_TOOLS:
        return
    # None = undeterminable: claim nothing (there is nothing to claim).
    targets = _targets(tool, content) or []
    ok = (isinstance(result, dict) and not result.get("error") and not result.get("blocked")
          and result.get("exit_code") in (None, 0))
    guard.registry.settle(guard.worker, targets, ok)
    if ok:
        guard.registry.claim(guard.worker, targets)


# ---------------------------------------------------------------------------
# v2: stop / steer one worker; the live registry behind /api/chat/activity
# ---------------------------------------------------------------------------

_ACTIVE_WORKERS: Dict[str, asyncio.Task] = {}   # child session id → the worker's task
_WORKER_RUNS: Dict[str, "SubagentRun"] = {}     # child session id → its SubagentRun


def _setting(key: str, default: Any = None) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# One GPU per machine, not one per delegate_agents CALL: the "at most N
# workers generate at once" semaphore is shared by every delegation on the
# same endpoint (a chat's /agents and two dispatched jobs at the same time
# used to run 3 × N workers against one Ollama, all queueing on the model's
# single slot while each worker's wall-clock timeout ticked). Keyed by the
# event loop too — asyncio primitives bind to the loop that first waits on
# them, and the test suite runs one loop per test.
_SLOTS: Dict[tuple, asyncio.Semaphore] = {}


def shared_slots(endpoint_url: str, size: int) -> Optional[asyncio.Semaphore]:
    if size <= 0:
        return None
    try:
        loop_key = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_key = 0
    try:
        from urllib.parse import urlparse
        host = (urlparse(endpoint_url or "").netloc or endpoint_url or "").lower()
    except Exception:
        host = str(endpoint_url or "")
    key = (loop_key, host, int(size))
    sem = _SLOTS.get(key)
    if sem is None:
        # a different size for the same host means the setting changed:
        # forget the old semaphores of that host (their waiters finish on them)
        for k in [k for k in _SLOTS if k[0] == loop_key and k[1] == host]:
            _SLOTS.pop(k, None)
        sem = _SLOTS[key] = asyncio.Semaphore(int(size))
    return sem


def _cancel_task(task: asyncio.Task) -> None:
    """Cancel from whatever thread we are on (FastAPI runs `def` routes in a
    threadpool; a bare cancel() from there can be lost)."""
    try:
        from src.agent_runs import _cancel_anywhere
        _cancel_anywhere(task)
    except Exception:
        task.cancel()


def stop_worker(child_session_id: str, reason: str = "stopped") -> bool:
    """Cancel ONE worker; the coordinator carries on with the others.
    `reason` becomes the worker's stop_reason ("stopped" for the user's Stop,
    "stalled" for the supervisor)."""
    task = _ACTIVE_WORKERS.get(child_session_id)
    if task is None or task.done():
        return False
    # Mark the cancellation as targeting THIS worker. Without the flag, `one()`
    # cannot tell it apart from the coordinator's own cancellation (Stop), and
    # swallowing the latter let the delegation carry on after Stop.
    run = _WORKER_RUNS.get(child_session_id)
    if run is not None:
        run.stop_requested = True
        run.stop_reason_requested = str(reason or "stopped")
    _cancel_task(task)
    return True


def active_worker_ids() -> List[str]:
    return [sid for sid, t in _ACTIVE_WORKERS.items() if not t.done()]


def steer_worker(child_session_id: str, text: str, source: str = "user") -> bool:
    """Queue a steering message for a live worker. The worker's agent loop
    injects it as a `user` message before its next round (see
    `stream_agent_loop(pending_user_messages=...)`)."""
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return False
    task = _ACTIVE_WORKERS.get(child_session_id)
    run = _WORKER_RUNS.get(child_session_id)
    if task is None or task.done() or run is None:
        return False
    run.steer_queue.append({"text": text[:4000], "source": "supervisor" if source == "supervisor" else "user"})
    return True


def pending_steers(child_session_id: str) -> List[Dict[str, str]]:
    """Drain the steering queue of a worker (what the loop injects next)."""
    run = _WORKER_RUNS.get(child_session_id)
    if run is None:
        return []
    out, run.steer_queue = list(run.steer_queue), []
    return out


def worker_board() -> Dict[str, Dict[str, Any]]:
    """Live workers for /api/chat/activity: child sid → a small status card."""
    out: Dict[str, Dict[str, Any]] = {}
    for sid, run in list(_WORKER_RUNS.items()):
        task = _ACTIVE_WORKERS.get(sid)
        if task is None or task.done():
            continue
        out[sid] = {
            "parent": run.parent_session_id, "name": run.name, "role": run.role,
            "started_at": run.started, "round": run.rounds, "last_event_at": run.last_event_at,
            "stalled": bool(run.stalled), "tool_calls": run.tool_calls,
        }
    return out


def _as_bool(value: Any, default: bool) -> bool:
    """`bool("false")` is True. Models send booleans as strings."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "si", "sí"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return default


_TASK_FILES_RE = re.compile(r"^\s*\[([^\]]+)\]\s*")
_TASK_MODEL_RE = re.compile(r"^\s*\{([^}]+)\}\s*")
# Tools a worker must not use: no recursion, no user prompts (nobody is
# watching the child chat), no session management.
SUBAGENT_DISABLED_TOOLS = frozenset({
    "delegate_agents", "ask_user", "create_session", "send_to_session",
    "manage_session", "list_sessions", "pipeline", "chat_with_model",
    "ask_teacher", "update_plan",
})

# Tools a scoped worker never needs but that the retriever happily hands it
# (measured: 19 schemas = 4.7k tokens, 65 % of a worker's first round on a
# 9B model). Removed when `agent_subagent_lean_tools` is on (default) unless
# the task text itself asks for the web / memory / background jobs.
SUBAGENT_LEAN_DENYLIST = frozenset({
    "web_search", "web_fetch", "manage_skills", "manage_bg_jobs", "manage_memory",
    "manage_tasks", "manage_contact", "ui_control", "manage_notes", "project_context",
})
# Which family of lean-denied tools a task text asks for. Per family, on
# purpose: one keyword ("memoria" in "fuga de memoria") used to restore all
# ten tools at once, web search and background jobs included.
_LEAN_KEEP_FAMILIES = (
    (re.compile(r"\b(web|internet|url|https?://|busca en|search the|fetch|descarga|download)\b", re.I),
     frozenset({"web_search", "web_fetch"})),
    (re.compile(r"\b(memoria|memory|recuerda|remember)\b", re.I), frozenset({"manage_memory"})),
    (re.compile(r"\bskills?\b", re.I), frozenset({"manage_skills"})),
    (re.compile(r"\b(background( job)?|segundo plano|bg job)\b", re.I), frozenset({"manage_bg_jobs"})),
    (re.compile(r"\b(contact[os]?|contacts?)\b", re.I), frozenset({"manage_contact"})),
    (re.compile(r"\b(notes?|notas?|apunte)\b", re.I), frozenset({"manage_notes"})),
    (re.compile(r"\b(todo list|tareas pendientes|task list)\b", re.I), frozenset({"manage_tasks"})),
)
# Kept for callers that import it: every keyword of every family.
_LEAN_KEEP_RE = re.compile("|".join(f"(?:{rx.pattern})" for rx, _ in _LEAN_KEEP_FAMILIES), re.I)


def worker_disabled_tools(instruction: str, permissions: Any = None) -> set:
    """The worker's denylist: the hard set plus, when the lean mode is on,
    the tools a scoped worker never needs — minus the family (web, memory,
    skills, background jobs, contacts, notes, tasks) the task text asks for.

    With an agent definition's derived permissions (src/subagent_permissions.py)
    its own denials are added, its allowlist becomes the deny of everything
    else, and `delegate_agents` comes BACK out of the hard set — the only way
    out of that set, and only ever for a definition a human wrote.

    The lean denylist is a GUESS about what this task needs; a definition's
    `tools` list is a statement about what this agent is. So a name the
    definition allows is taken back out of the lean guess: keeping it would
    let a keyword in the instruction decide what a human already decided.

    None of this is the last word. The agent loop's workspace tool floor
    deliberately restores read_file / ls / edit_file / apply_patch for a bound
    folder — a worker that cannot read its own project is not a worker — which
    would quietly hand a read-only reviewer the edit path back. That is why
    the definitions are ALSO enforced at execution time, in
    :func:`write_block_reason`, where no floor reaches.
    """
    out = set(SUBAGENT_DISABLED_TOOLS)
    allowed = getattr(permissions, "allowed_tools", None) if permissions is not None else None
    if _as_bool(_setting("agent_subagent_lean_tools", True), True):
        keep: set = set()
        text = str(instruction or "")
        for rx, tools in _LEAN_KEEP_FAMILIES:
            if rx.search(text):
                keep |= tools
        lean = set(SUBAGENT_LEAN_DENYLIST) - keep
        if allowed is not None:
            lean -= set(allowed)
        out |= lean
    if permissions is None:
        return out
    out |= set(getattr(permissions, "denied_tools", ()) or ())
    if allowed is not None:
        try:
            from src.agent_defs import known_tools
            out |= {t for t in known_tools() if t not in allowed}
        except Exception:  # noqa: BLE001 - vocabulary unavailable; the
            pass          # execution guard still refuses what is not allowed
    if getattr(permissions, "may_delegate", False):
        out.discard("delegate_agents")
    return out


def _short(text: Any, n: int = 160) -> str:
    s = str(text or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _active_workspace_or_none() -> Optional[str]:
    """The bound folder, for the agent definitions a repo may carry. Best
    effort: outside a turn there is none and the repo definitions simply do
    not exist, which is the same answer the trust gate would give."""
    try:
        from src.tool_execution import get_active_workspace
        return get_active_workspace() or None
    except Exception:  # noqa: BLE001 - no turn, no workspace
        return None


def _resume_handle(raw: Any) -> Optional[Dict[str, str]]:
    """``{"kind","id","runner"}`` for a worker to be CONTINUED rather than
    rebuilt, or None. A bare string is read as a chat session id."""
    if isinstance(raw, str) and raw.strip():
        return {"kind": "session", "id": raw.strip()[:120], "runner": ""}
    if isinstance(raw, dict):
        ident = str(raw.get("id") or raw.get("session_id") or "").strip()
        if not ident:
            return None
        kind = str(raw.get("kind") or "session").strip().lower()
        return {"kind": ("runner" if kind == "runner" else "session"), "id": ident[:120],
                "runner": str(raw.get("runner") or "").strip()[:60]}
    return None


def parse_delegation_args(content: str, *, workspace: Optional[str] = None) -> Dict[str, Any]:
    """Accept {"tasks":[{"name","instruction"}...], "parallel": bool,
    "max_rounds": int} — or, leniently, a plain list of instruction strings.

    A task may also name an `agent` (src/agent_defs.py): the definition fills
    in the model, the runner, the system prompt, the tool allowlist, the
    permission rules and the default file claims, and anything the task states
    explicitly still wins. A payload that names NO agent produces exactly the
    dict this parser has always produced, key for key — that is a test
    (tests/test_agent_defs.py), not an intention.
    """
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
    if isinstance(tasks_raw, str) and tasks_raw.strip().startswith(("[", "{")):
        # qwen3.5 (native tool calls) sometimes double-encodes the list —
        # {"tasks": "[{...}, {...}]"} — or even stuffs the rest of the object
        # into that string: {"tasks": "[{...}], \"parallel\": true, \"reviewer\": true}"}.
        # Seen live on the bench: it cost a failed call, a correction round
        # (which dropped the reviewer flag) and a second approval.
        text = tasks_raw.strip()
        try:
            parsed, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            parsed, end = None, 0
        if parsed is not None:
            tasks_raw = [parsed] if isinstance(parsed, dict) else parsed
            rest = text[end:].strip().lstrip(",").strip()
            if rest.endswith("}") and not rest.startswith("{"):
                rest = "{" + rest
            if rest.startswith("{"):
                try:
                    extra = json.loads(rest)
                except json.JSONDecodeError:
                    extra = None
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        data.setdefault(k, v)
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise ValueError("delegate_agents: 'tasks' must be a non-empty list")
    tasks: List[Dict[str, Any]] = []
    for i, t in enumerate(tasks_raw[:MAX_SUBAGENTS]):
        files: List[str] = []
        model = ""
        agent = ""
        resume: Optional[Dict[str, str]] = None
        if isinstance(t, str):
            instruction, name = t.strip(), ""
        elif isinstance(t, dict):
            agent = str(t.get("agent") or "").strip()[:60]
            resume = _resume_handle(t.get("resume"))
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
        row: Dict[str, Any] = {"name": name[:80], "instruction": instruction[:8000],
                               "model": model[:120], "files": files}
        # `agent` and `resume` are carried ONLY when the caller named one —
        # the discipline `runner` already keeps in src/dispatch.py. A task with
        # neither key must produce the dict this parser produced before they
        # existed, because a coordinator that never heard of agent definitions
        # has to keep working byte for byte.
        if agent:
            row["agent"] = agent
        if resume:
            row["resume"] = resume
        tasks.append(row)
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
    dropped = max(0, len(tasks_raw) - MAX_SUBAGENTS)
    if dropped:
        logger.info("delegate_agents: %s tasks requested, capped at %s", len(tasks_raw), MAX_SUBAGENTS)
    try:
        max_rounds = int(data.get("max_rounds") or DEFAULT_MAX_ROUNDS)
    except (TypeError, ValueError):
        max_rounds = DEFAULT_MAX_ROUNDS
    try:
        timeout_s = float(data.get("timeout_s") or data.get("timeout") or DEFAULT_WORKER_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = float(DEFAULT_WORKER_TIMEOUT_S)
    timeout_s = max(MIN_WORKER_TIMEOUT_S, min(timeout_s, 7200))
    job_agent = str(data.get("agent") or "").strip()[:60]
    if job_agent:
        for row in tasks:
            row.setdefault("agent", job_agent)
    reviewer_agent = str(data.get("reviewer_agent") or "").strip()[:60]
    out = {
        "tasks": tasks,
        "parallel": _as_bool(data.get("parallel"), True),
        "max_rounds": max(3, min(max_rounds, 40)),
        "shared_context": str(data.get("context") or data.get("shared_context") or "").strip()[:4000],
        "timeout_s": int(timeout_s) if float(timeout_s).is_integer() else timeout_s,
        "reviewer": _as_bool(reviewer, False),
        "reviewer_model": reviewer_model[:120],
        # Tasks past MAX_SUBAGENTS are not run; the tool result says so (they
        # used to vanish silently and the model believed they were done).
        "dropped_tasks": dropped,
    }
    if any(row.get("agent") for row in tasks) or reviewer_agent:
        _apply_agent_defs(out, reviewer_agent,
                          workspace if workspace is not None else _active_workspace_or_none())
    return out


def _apply_agent_defs(args: Dict[str, Any], reviewer_agent: str, workspace: Optional[str]) -> None:
    """Fill the tasks in from the definitions they name.

    Called only when something named an agent, so a payload that names none
    never reaches this path at all.

    A definition that does not resolve REFUSES the call. Running the task as
    the plain, unrestricted worker it would otherwise have been is the one
    outcome that must not happen quietly: the caller asked for a reviewer that
    cannot write and would have got a worker that can. Nothing has started
    when this runs, so refusing costs the other tasks nothing but a corrected
    spelling.
    """
    from src import agent_defs
    errors = agent_defs.resolve_tasks(args.get("tasks") or (), workspace=workspace)
    if errors:
        raise ValueError("delegate_agents: " + "; ".join(e["reason"] for e in errors)
                         + ". A task whose agent definition is missing would run as an ordinary "
                           "worker with none of its restrictions, so the call is refused instead.")
    if not reviewer_agent:
        return
    definition = agent_defs.get(reviewer_agent, workspace)
    if definition is None:
        raise ValueError(f"delegate_agents: unknown agent definition for the reviewer: "
                         f"{reviewer_agent!r}")
    if definition.mode != "reviewer":
        raise ValueError(f"delegate_agents: agent `{definition.slug}` has mode `{definition.mode}`, "
                         f"so it cannot fill the reviewer slot — the reviewer runs over everyone "
                         f"else's work with the file locks off, and only a definition that says "
                         f"`mode: reviewer` may.")
    args["reviewer_agent"] = definition.slug
    args["reviewer"] = True


class SubagentRun:
    def __init__(self, index: int, task: Dict[str, Any], role: str = "worker"):
        self.index = index
        self.id = f"sa{index + 1}-{uuid.uuid4().hex[:6]}"
        self.name = task["name"]
        self.instruction = task["instruction"]
        self.model_override = task.get("model") or ""
        self.files: List[str] = list(task.get("files") or [])
        self.role = role
        # ── the agent definition this worker came from (src/agent_defs.py) ──
        # Every one of these is empty/None for a task that named no agent, and
        # each is read at exactly one enforcement point below.
        self.agent = str(task.get("agent") or "")
        self.agent_def: Optional[Dict[str, Any]] = task.get("agent_def") if isinstance(task.get("agent_def"), dict) else None
        self.system_prompt = str(task.get("system_prompt") or "")
        self.endpoint_id = str(task.get("endpoint_id") or "")
        self.max_rounds_override = task.get("max_rounds") or None
        self.timeout_s_override = task.get("timeout_s") or None
        #: Derived by DelegateAgentsTool before the run starts; None means an
        #: unrestricted worker, i.e. exactly today's behaviour.
        self.permissions: Any = None
        #: The reviewer that runs AFTER everyone bypasses the file locks
        #: because nobody else is still writing. That is a fact about WHEN it
        #: runs, so it is set by the caller that schedules it — never derived
        #: from `role`, which a definition can also set.
        self.bypass_locks = False
        #: Continue a worker instead of rebuilding one (`resume`): the child
        #: chat session a previous round already used, or an external runner's
        #: own session handle.
        resume = task.get("resume") if isinstance(task.get("resume"), dict) else None
        self.resume_kind = str((resume or {}).get("kind") or "")
        self.resume_id = str((resume or {}).get("id") or "")
        self.resumed = False
        #: An external runner's session handle, as reported by the run. Carried
        #: so a later round can reach THAT agent rather than a fresh one.
        self.runner_session = ""
        self.stopped_by_user = False
        # Set by stop_worker(): "the cancellation about to arrive targets ME",
        # as opposed to the coordinator being cancelled (the user pressed Stop).
        self.stop_requested = False
        self.stop_reason_requested: Optional[str] = None
        self.session_id: Optional[str] = None
        self.parent_session_id: Optional[str] = None
        self.text = ""
        self.tool_calls = 0
        self.failed_calls = 0
        self.mutations: List[str] = []
        self.rejections = 0
        self.stop_reason = "unknown"
        self.error: Optional[str] = None
        # `started` is reset when the worker really starts (after queueing).
        self.started = time.time()
        self.finished: Optional[float] = None
        self.static_checks: List[Dict[str, Any]] = []
        self.git: Optional[Dict[str, Any]] = None
        self.rounds = 0
        self.summary: Optional[Dict[str, Any]] = None
        # Control board state (fed by the worker's own events; read by the
        # watchdog tick, the supervisor and /api/chat/activity).
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_event_at = self.started      # last real worker event (not ticks)
        self.last_tool: Optional[str] = None
        self.last_tool_sig: Optional[str] = None
        self.repeat_count = 0                  # same tool + same args, consecutively
        self.stalled = False
        self.stall_reason: Optional[str] = None
        self.steer_queue: List[Dict[str, str]] = []
        self.steered = 0
        self.supervisor: List[Dict[str, Any]] = []
        self.final_metrics: Dict[str, Any] = {}
        self.tool_events: List[Dict[str, Any]] = []

    def touch(self) -> None:
        self.last_event_at = time.time()

    @property
    def loop_detected(self) -> bool:
        return self.repeat_count >= 3

    def note_tool_start(self, tool: Any, command: Any) -> None:
        """Loop detection: the same tool with the same command/args, three
        times in a row, is a stall (the model is not making progress)."""
        self.last_tool = str(tool or "") or None
        sig = f"{tool}\x00{str(command or '')[:2000]}"
        self.repeat_count = self.repeat_count + 1 if sig == self.last_tool_sig else 1
        self.last_tool_sig = sig

    def outcome(self) -> Optional[str]:
        """The four-value outcome of this run (src/tool_outcome.py): a worker
        the user stopped is `cancelled`, not a failed one. None while the
        `agent_tool_outcomes` setting is off."""
        try:
            from src import tool_outcome
            if not tool_outcome.enabled():
                return None
            return tool_outcome.classify_status(
                self.stop_reason, error=self.error,
                cancelled=bool(self.stopped_by_user and self.stop_reason in ("stopped", "cancelled")),
            ).value
        except Exception:  # noqa: BLE001 - a report is never worth an exception
            return None

    def report(self) -> Dict[str, Any]:
        outcome = self.outcome()
        return {
            "id": self.id, "name": self.name, "session_id": self.session_id,
            "status": "error" if self.error else ("done" if self.stop_reason in ("complete",) else self.stop_reason),
            **({"outcome": outcome} if outcome else {}),
            "stop_reason": self.stop_reason, "error": self.error,
            "tool_calls": self.tool_calls, "failed_calls": self.failed_calls,
            "mutations": self.mutations, "rejections": self.rejections, "rounds": self.rounds,
            "static_checks": self.static_checks, "git": self.git,
            "duration_s": round((self.finished or time.time()) - self.started, 1),
            "final_text": self.text.strip()[:2500],
            "role": self.role, "files": self.files, "model": self.model_override or None,
            "instruction": self.instruction[:2000],
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "started_at": self.started, "ended_at": self.finished,
            "steered": self.steered, "supervisor": list(self.supervisor),
            # Conditional, like `outcome` above: a worker with no definition
            # reports the dict it has always reported.
            **({"agent": self.agent} if self.agent else {}),
            **({"agent_def": self.agent_def} if self.agent_def else {}),
            **({"permissions": self.permissions.to_dict()} if self.permissions is not None else {}),
            **({"resumed": True, "resumed_from": self.resume_id} if self.resumed else {}),
            **({"runner_session": self.runner_session} if self.runner_session else {}),
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
    timeout_s: Optional[float] = None,
    save_transcript: bool = True,
) -> None:
    """Run one worker to completion (or until cancelled) and stream its board
    events through `emit`. `save_transcript=False` leaves the child-chat
    transcript to the caller (`one()` saves it AFTER the stop reason is final:
    saved here, in the `finally`, a stopped/timed-out worker was recorded as
    if it had completed)."""
    from src.agent_loop import stream_agent_loop
    from src.ai_interaction import get_session_manager

    # Exclusive files: pre-claim the declared ones, then first-writer-wins.
    # The lock key is run.id (unique), not run.name (written by the model).
    if locks is not None:
        locks.names[run.id] = run.name
        taken = locks.claim(run.id, run.files) if run.files else []
        if taken:
            logger.info("delegate_agents: %s wanted %s but they belong to another worker", run.name, taken)
        # `bypass_locks` is set by whoever SCHEDULED the reviewer slot, not read
        # off `run.role`: an agent definition can say `mode: reviewer` too, and
        # a definition-driven reviewer running as an ordinary task runs
        # alongside the others — handing it the bypass would let it clobber the
        # very files it was started to look at.
        _LOCK_CTX.set(_LockGuard(locks, run.id, bypass=bool(run.bypass_locks)))
    # Inherited by every tool task the agent loop spawns, the same way the lock
    # guard is; None for a worker with no definition, which is the whole of
    # what keeps an ordinary delegation on its old path.
    _PERMS_CTX.set(run.permissions)

    sm = get_session_manager()
    parent_name = ""
    if sm and parent_session_id:
        try:
            parent = sm.get_session(parent_session_id)
            parent_name = getattr(parent, "name", "") or ""
        except Exception:
            parent_name = ""
    # Resume: continue the worker that made the change, in its own session,
    # rather than building a new one from the original task plus the failure
    # text. The expensive half of a fix round is re-deriving context the first
    # worker already had — the files it read, the model of the problem it
    # built. `prior` is what that session already holds; with none of it (the
    # session was pruned, the manager has no history) this degrades silently to
    # a fresh worker, which is exactly today's behaviour.
    prior: List[Dict[str, Any]] = []
    if run.resume_kind == "session" and run.resume_id and sm:
        prior = _session_messages(sm, run.resume_id)
        if prior:
            run.resumed = True
    child_sid = run.resume_id if run.resumed else str(uuid.uuid4())[:8]
    run.session_id = child_sid
    run.parent_session_id = parent_session_id
    # The worker starts NOW (a queued worker waited before this point).
    run.started = time.time()
    run.last_event_at = run.started
    if sm and not run.resumed:
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

    if run.system_prompt:
        # The definition's body IS the system prompt (src/agent_defs.py). It
        # replaces the built-in preamble rather than being appended to it: two
        # descriptions of the same job, disagreeing, is how a worker ends up
        # doing neither.
        preamble = run.system_prompt
    elif run.role == "reviewer":
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
    if run.system_prompt and run.files:
        # The lock sentence is mechanical, not editorial: a definition author
        # cannot know which files this particular task was given, so it is
        # appended to their prompt rather than replaced by it.
        preamble += (
            "\n\nFILES YOU OWN (exclusive — other workers cannot write them, and you must not write "
            "any file owned by another worker): " + ", ".join(run.files[:40])
        )
    if run.permissions is not None:
        blocked = sorted(run.permissions.denied_tools)[:12]
        if blocked:
            preamble += ("\n\nTools you do NOT have in this run: " + ", ".join(blocked)
                         + ". They are refused at the point of use, so do not plan around calling them.")
    if shared_context:
        preamble += "\n\nShared context from the coordinator:\n" + shared_context
    if run.resumed:
        messages = prior + [{"role": "user", "content":
                             "Same session, next round.\n\nYOUR TASK: " + run.instruction}]
    else:
        messages = [{"role": "user", "content": f"{preamble}\n\nYOUR TASK: {run.instruction}"}]

    await emit({"event": "started", "name": run.name, "instruction": _short(run.instruction, 240), "session_id": child_sid,
                "role": run.role, "files": run.files, "model": run.model_override or model,
                "started_at": run.started, "max_rounds": max_rounds, "timeout_s": timeout_s,
                # A worker card must be able to say which definition it came
                # from; a run whose authority is invisible cannot be audited.
                **({"agent": run.agent} if run.agent else {}),
                **({"resumed_from": run.resume_id} if run.resumed else {})})
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
    # Tokens: per-round usage arrives in `round_info` while the worker runs
    # (the tick shows it live); the final `metrics` totals win when present.
    _tokens_from_metrics = False

    def _steers() -> List[Dict[str, str]]:
        return pending_steers(child_sid)

    try:
        async for chunk in stream_agent_loop(
            endpoint_url, model, messages,
            headers=headers, temperature=0.3, max_tokens=0,
            max_rounds=max_rounds, session_id=child_sid, owner=owner,
            workspace=workspace, workspace_roots=workspace_roots,
            disabled_tools=worker_disabled_tools(run.instruction, run.permissions),
            security_gate_bypass=True, _is_teacher_run=True,
            gen_overrides=gen_overrides,
            harness_options=_worker_opts,
            pending_user_messages=_steers,
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
            run.touch()
            et = ev.get("type")
            if "delta" in ev and not et:
                if not ev.get("thinking"):
                    run.text += ev["delta"]
                continue
            if et == "tool_start":
                run.note_tool_start(ev.get("tool"), ev.get("full_command") or ev.get("command"))
                await emit({"event": "tool", "tool": ev.get("tool"), "command": _short(ev.get("command"), 120), "phase": "start"})
            elif et == "tool_progress":
                # The worker's own bash/python live tail.
                await emit({"event": "tool", "phase": "progress", "tool": ev.get("tool"),
                            "elapsed_s": float(ev.get("elapsed_s") or ev.get("elapsed") or 0),
                            "tail": _short(ev.get("tail") or ev.get("message"), 200)})
            elif et == "tool_output":
                run.tool_calls += 1
                ok = ev.get("exit_code") in (0, None)
                if not ok:
                    run.failed_calls += 1
                run.tool_events.append({"tool": ev.get("tool"), "command": ev.get("command"), "output": _short(ev.get("output"), 400), "exit_code": ev.get("exit_code")})
                await emit({"event": "tool", "tool": ev.get("tool"), "ok": ok, "phase": "done", "output": _short(ev.get("output"), 120)})
            elif et == "round_info":
                run.rounds = max(run.rounds, int(ev.get("round") or 0))
                if not _tokens_from_metrics:
                    run.input_tokens += int(ev.get("input_tokens") or 0)
                    run.output_tokens += int(ev.get("output_tokens") or 0)
                await emit({"event": "round", "round": run.rounds})
            elif et == "steer":
                run.steered += 1
                await emit({"event": "steer", "text": _short(ev.get("text"), 300),
                            "source": "supervisor" if ev.get("source") == "supervisor" else "user"})
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
                run.final_metrics = ev.get("data") or {}
                if run.stop_reason == "unknown":
                    run.stop_reason = ((run.final_metrics.get("harness") or {}).get("stop_reason")) or "complete"
                _in, _out = run.final_metrics.get("input_tokens"), run.final_metrics.get("output_tokens")
                if isinstance(_in, (int, float)) or isinstance(_out, (int, float)):
                    run.input_tokens = int(_in or 0)
                    run.output_tokens = int(_out or 0)
                    _tokens_from_metrics = True
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
        if save_transcript:
            _save_transcript(run, sm)
    await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300)})


#: How much of a resumed session is replayed into the next round. The point of
#: resuming is the context the worker already built, not its whole transcript:
#: a 20-round worker's history would not fit and the tail is where the state is.
RESUME_MESSAGES = 12
RESUME_CHARS = 4000


def _session_messages(sm: Any, session_id: str) -> List[Dict[str, Any]]:
    """The tail of a child worker's own chat, as loop messages.

    Empty for every reason a session can be unavailable — pruned, never
    created, a manager that keeps no history — and an empty list is what makes
    the caller fall back to a fresh worker instead of failing. Resume is a
    saving, never a dependency.
    """
    try:
        session = sm.get_session(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("delegate_agents: resume lookup for %s failed: %s", session_id, exc)
        return []
    if session is None:
        return []
    raw = list(getattr(session, "messages", None) or ())[-RESUME_MESSAGES:]
    out: List[Dict[str, Any]] = []
    for message in raw:
        role = str(getattr(message, "role", "") or (message.get("role") if isinstance(message, dict) else ""))
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        text = str(content or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        out.append({"role": role, "content": text[:RESUME_CHARS]})
    return out


def _save_transcript(run: SubagentRun, sm: Any) -> None:
    """Persist the worker's transcript into its child chat so it can be
    audited later — also when it was stopped, stalled or timed out: what it
    did is evidence, and HOW it ended is recorded in the metadata."""
    if not sm or not run.session_id:
        return
    try:
        from core.models import ChatMessage
        child = sm.get_session(run.session_id)
        if child is None:
            return
        child.add_message(ChatMessage("user", run.instruction))
        meta = dict(run.final_metrics or {})
        meta["tool_events"] = run.tool_events[:60]
        meta["subagent"] = {
            "parent_session": run.parent_session_id, "name": run.name, "role": run.role,
            "stop_reason": run.stop_reason, "steered": run.steered,
            "supervisor": list(run.supervisor),
        }
        outcome = run.outcome()
        if outcome:
            meta["subagent"]["outcome"] = outcome
        if run.error:
            meta["subagent"]["error"] = run.error
        if run.stop_reason in ("stopped", "stalled", "timeout"):
            meta["stopped"] = True
            if run.stop_reason == "timeout":
                meta["subagent"]["timeout"] = True
        child.add_message(ChatMessage("assistant", run.text.strip() or "(no final text)", metadata=meta))
        sm.save_sessions()
    except Exception as e:
        logger.debug("delegate_agents: transcript save failed: %s", e)


def _build_report_text(runs: List[SubagentRun], workspace: Optional[str], locks: Optional[FileLockRegistry] = None) -> str:
    workers = [r for r in runs if r.role != "reviewer"]
    lines = [f"Delegated {len(workers)} sub-agent task(s). Evidence-based report (from tool logs, not from the workers' prose):"]
    for r in runs:
        rep = r.report()
        status = "ERROR" if r.error else r.stop_reason.upper()
        lines.append("")
        tag = "REVIEWER" if r.role == "reviewer" else f"[{r.index + 1}]"
        tokens = f", {r.input_tokens}+{r.output_tokens} tokens" if (r.input_tokens or r.output_tokens) else ""
        lines.append(f"## {tag} {r.name} — {status} in {rep['duration_s']}s, {r.rounds} rounds, {r.tool_calls} tool calls ({r.failed_calls} failed){tokens}, child chat {r.session_id}")
        if r.error:
            lines.append(f"   error: {r.error}")
        if r.stop_reason == "stalled":
            lines.append("   STALLED: the supervisor stopped this worker because it made no progress; its task is NOT done.")
        elif r.stop_reason == "stopped":
            lines.append("   STOPPED by the user before it finished; its task may be incomplete.")
        elif r.stop_reason == "timeout":
            lines.append("   TIMED OUT before it finished; its task may be incomplete.")
        for a in r.supervisor[:6]:
            lines.append(f"   supervisor {a.get('action')}: {a.get('reason')}")
        if r.steered:
            lines.append(f"   steering messages injected while it ran: {r.steered}")
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
    # Deduplicated by run.id: two tasks the model gave the SAME name used to
    # collapse into one "worker" here and the warning never appeared.
    by_id = {r.id: r for r in runs}
    seen: Dict[str, List[str]] = {}
    for r in runs:
        for p in r.mutations:
            key = str(p).replace("\\", "/").lower()
            ids = seen.setdefault(key, [])
            if r.id not in ids:
                ids.append(r.id)
    overlaps = {k: v for k, v in seen.items()
                if len(v) > 1 and not (locks is not None
                                       and any(by_id[i].role == "reviewer" for i in v))}
    if overlaps:
        lines.append("")
        lines.append("WARNING — files changed by MORE THAN ONE worker (review for clobbered edits):")
        for k, ids in list(overlaps.items())[:10]:
            lines.append(f"   {k}: " + ", ".join(by_id[i].name for i in ids))
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


def _parent_standing(ctx: dict, workspace: Optional[str], roots: Optional[List[str]]) -> Any:
    """The permissions of whoever is doing the delegating.

    Read from the CONTEXTVAR first, not from `ctx`: a nested delegation is a
    tool call inside a worker's own loop, and the context var is the one
    channel that reaches it without every intermediate layer agreeing to pass
    a dict along. A restriction that survives only when six call sites
    remember to forward it is not a restriction.
    """
    from src.subagent_permissions import coordinator_permissions
    parent = _PERMS_CTX.get()
    if parent is None:
        parent = ctx.get("permissions")
    if parent is not None:
        return parent
    return coordinator_permissions(workspace_roots=roots or (), workspace=str(workspace or ""))


def _attach_permissions(runs: List["SubagentRun"], ctx: dict, workspace: Optional[str],
                        roots: Optional[List[str]]) -> str:
    """Derive each worker's permissions before it starts. Returns the refusal
    to hand back, or "".

    A worker whose task named no definition, started by a parent with nothing
    on it, keeps ``permissions = None`` — the guard and the denylist both read
    None as "no definition", so an ordinary delegation runs the code it has
    always run.
    """
    parent = _parent_standing(ctx, workspace, roots)
    restricted_parent = bool(getattr(parent, "rules", ()) or getattr(parent, "denied_tools", ())
                             or getattr(parent, "allowed_tools", None) is not None)
    if not restricted_parent and not any(run.agent_def for run in runs):
        # Nothing to derive. Returning here keeps the tool vocabulary (and the
        # tool index behind it) out of the path of every ordinary delegation.
        return ""
    from src import agent_defs
    from src.subagent_permissions import DepthExceeded, derive
    vocabulary = agent_defs.known_tools()
    for run in runs:
        definition = agent_defs.from_dict(run.agent_def) if run.agent_def else None
        if definition is None and not restricted_parent:
            continue
        try:
            run.permissions = derive(parent, definition, parent_depth=int(getattr(parent, "depth", 0)),
                                     workspace_roots=roots or (), workspace=str(workspace or ""),
                                     vocabulary=vocabulary)
        except DepthExceeded as exc:
            return f"delegate_agents: {exc}"
        if run.agent_def is not None and run.permissions.caveats:
            run.agent_def = dict(run.agent_def, caveats=list(run.permissions.caveats))
    return ""


def _endpoint_for(run: "SubagentRun", default_url: str, owner: Optional[str]) -> str:
    """The endpoint one worker runs on: its definition's `endpoint_id` when
    that id resolves, the coordinator's otherwise.

    A fallback is never silent. An `endpoint_id` that does not resolve today
    (an endpoint that was deleted, disabled, or belongs to another owner) is a
    routing fact, not a permission, so the run proceeds — but it says on its
    own card that it did not run where the definition said it would.
    """
    if not run.endpoint_id:
        return default_url
    try:
        from src.endpoint_resolver import resolve_endpoint_by_id
        resolved = resolve_endpoint_by_id(run.endpoint_id, run.model_override or None, owner=owner)
    except Exception as exc:  # noqa: BLE001 - a route lookup never fails a run
        logger.debug("delegate_agents: endpoint %s unavailable: %s", run.endpoint_id, exc)
        resolved = None
    if resolved and resolved[0]:
        return str(resolved[0])
    note = (f"endpoint `{run.endpoint_id}` from the agent definition did not resolve; this worker ran "
            f"on the coordinator's endpoint instead")
    logger.info("delegate_agents: %s", note)
    if run.agent_def is not None:
        run.agent_def = dict(run.agent_def, caveats=list(run.agent_def.get("caveats") or []) + [note])
    return default_url


class DelegateAgentsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import get_active_workspace, get_active_workspace_roots
        try:
            args = parse_delegation_args(content, workspace=get_active_workspace() or None)
        except ValueError as e:
            return {"error": str(e), "exit_code": 1}
        parent_sid = ctx.get("session_id")
        owner = ctx.get("owner")
        progress_cb = ctx.get("progress_cb")
        from src.ai_interaction import get_session_manager
        sm = get_session_manager()
        parent = sm.get_session(parent_sid) if (sm and parent_sid) else None
        if parent is None:
            return {"error": "delegate_agents: parent chat session not found", "exit_code": 1}
        endpoint_url = str(getattr(parent, "endpoint_url", "") or "")
        model = str(getattr(parent, "model", "") or "")
        headers = getattr(parent, "headers", None) or None
        if not endpoint_url or not model:
            return {"error": "delegate_agents: parent session has no model route", "exit_code": 1}
        # Workers default to the coordinator's model unless the admin picked a
        # worker model (Settings → Agent & automation). Measured on the
        # two-card box: Ollama runs two DIFFERENT models' runners at the same
        # time (one request each in flight), but two requests to the SAME
        # model queue on its single slot — so a worker model of its own,
        # pinned to the other card in Local models → Options (main_gpu), is
        # what makes the coordinator and the workers overlap at all.
        # A caller that resolved the route itself (a dispatched job: it
        # reports `job.model` to the coordinator) passes it in ctx and it is
        # honoured as-is — the sub-agent setting is for CHATS, whose parent
        # model is the coordinator's, and it named the coordinator's endpoint.
        explicit_model = str(ctx.get("model") or "").strip()
        if explicit_model:
            model = explicit_model
        else:
            worker_model = str(_setting("agent_subagent_worker_model", "") or "").strip()
            if worker_model and worker_model.lower() != "auto":
                model = worker_model
        workspace = get_active_workspace()
        roots = list(get_active_workspace_roots() or ()) or None
        gen_overrides = ctx.get("gen_overrides") if isinstance(ctx.get("gen_overrides"), dict) else None

        runs = [SubagentRun(i, t) for i, t in enumerate(args["tasks"])]
        # The permissions of each worker, derived (never inherited) from the
        # parent's own standing plus the worker's definition. A task with no
        # definition and an unrestricted parent derives to None and takes the
        # path it always took.
        depth_error = _attach_permissions(runs, ctx, workspace, roots)
        if depth_error:
            return {"error": depth_error, "exit_code": 1}
        # One id per delegate_agents CALL: the board keys its state by it, so
        # a second /agents in the same chat does not pile onto the first.
        delegation_id = uuid.uuid4().hex[:8]
        locks = FileLockRegistry(workspace)
        harness_options = ctx.get("harness_options") if isinstance(ctx.get("harness_options"), dict) else None

        async def emit_for(run: SubagentRun):
            async def _emit(payload: Dict[str, Any]):
                if progress_cb is None:
                    return
                try:
                    await progress_cb({"subagent": {
                        "id": run.id, "index": run.index, "name": run.name, "role": run.role,
                        "ts": time.time(), "session_id": run.session_id, "delegation": delegation_id,
                        **payload,
                    }})
                except Exception:
                    pass
            return _emit

        # One GPU: at most N workers generate at the same time; the rest wait
        # ("queued") and only get `started` — and their timeout — when they run.
        try:
            max_parallel = int(_setting("agent_subagent_max_parallel", 2) or 0)
        except (TypeError, ValueError):
            max_parallel = 2
        slots = shared_slots(endpoint_url, max(1, max_parallel)) if max_parallel > 0 else None

        async def watchdog(run: SubagentRun, emit) -> None:
            """Heartbeat + deterministic supervisor. Emits a `tick` every
            agent_subagent_tick_seconds while the worker runs; a worker idle
            for agent_subagent_stall_seconds (or looping on one tool call) is
            nudged once with a steering message, and stopped if it is still
            stalled one stall period later. No LLM calls."""
            try:
                tick_s = float(_setting("agent_subagent_tick_seconds", 5) or 5)
                stall_s = float(_setting("agent_subagent_stall_seconds", 120) or 120)
            except (TypeError, ValueError):
                tick_s, stall_s = 5.0, 120.0
            tick_s = max(0.05, tick_s)
            supervise = _as_bool(_setting("agent_subagent_supervisor", True), True)
            nudged_at: Optional[float] = None
            while True:
                await asyncio.sleep(tick_s)
                if run.finished is not None:
                    return
                now = time.time()
                idle = max(0.0, now - run.last_event_at)
                if run.loop_detected:
                    reason = "loop"
                elif idle > stall_s:
                    reason = "idle"
                else:
                    reason = None
                run.stalled = reason is not None
                run.stall_reason = reason
                await emit({
                    "event": "tick", "elapsed_s": round(now - run.started, 2), "idle_s": round(idle, 2),
                    "round": run.rounds, "last_tool": run.last_tool, "tool_calls": run.tool_calls,
                    "input_tokens": run.input_tokens, "output_tokens": run.output_tokens,
                    "stalled": run.stalled, "stall_reason": reason,
                })
                if not (supervise and run.stalled and run.session_id):
                    continue
                if nudged_at is None:
                    detail = (f"loop: the same tool call issued {run.repeat_count} times in a row"
                              if reason == "loop" else f"idle: no activity for {int(idle)}s")
                    # (workers cannot ask_user — they run detached — so the
                    # way out is: finish, report the blocker, or change tack)
                    text = (f"You appear stuck: {detail}. Finish with what you have and report what "
                            "blocks you, or take a different approach.")
                    steer_worker(run.session_id, text, source="supervisor")
                    run.supervisor.append({"action": "nudge", "reason": detail, "ts": now})
                    nudged_at = now
                    await emit({"event": "supervisor", "action": "nudge", "reason": detail})
                elif now - nudged_at > stall_s:
                    detail = f"still stalled ({reason}) {int(now - nudged_at)}s after the nudge"
                    run.supervisor.append({"action": "stop", "reason": detail, "ts": now})
                    await emit({"event": "supervisor", "action": "stop", "reason": detail})
                    stop_worker(run.session_id, reason="stalled")
                    return

        async def one(run: SubagentRun, max_rounds: Optional[int] = None):
            emit = await emit_for(run)
            dog: Optional[asyncio.Task] = None
            queued = slots is not None and slots.locked()
            if queued:
                await emit({"event": "queued"})
            # A definition's own ceilings, when it has them. Both are already
            # clamped to the delegation parser's own bounds at load time, so a
            # definition can narrow a worker but never buy it more than a task
            # could have asked for directly.
            rounds = run.max_rounds_override or max_rounds or args["max_rounds"]
            limit = float(run.timeout_s_override or args["timeout_s"])
            # A definition may name the endpoint its worker runs on. The GPU
            # slot stays keyed on the COORDINATOR's endpoint on purpose: it
            # bounds how many workers generate at once on this box, and reading
            # it per-endpoint would raise that bound rather than honour it.
            worker_url = _endpoint_for(run, endpoint_url, owner)
            try:
                if slots is not None:
                    await slots.acquire()
                try:
                    dog = asyncio.create_task(watchdog(run, emit))
                    # Wall-clock bound per worker: a worker stuck on a foreground
                    # server or a silent model must not hang the coordinator.
                    # Counted from here — queue time is not the worker's.
                    await asyncio.wait_for(
                        _run_subagent(
                            run,
                            endpoint_url=worker_url, model=run.model_override or model, headers=headers, owner=owner,
                            workspace=workspace, workspace_roots=roots, max_rounds=rounds,
                            shared_context=args["shared_context"], parent_session_id=parent_sid,
                            emit=emit, gen_overrides=gen_overrides, locks=locks, harness_options=harness_options,
                            timeout_s=limit, save_transcript=False,
                        ),
                        timeout=limit,
                    )
                finally:
                    if slots is not None:
                        slots.release()
            except asyncio.TimeoutError:
                run.error = run.error or f"worker timed out after {limit}s (its running command was killed)"
                run.stop_reason = "timeout"
                run.finished = run.finished or time.time()
                await emit({"event": "error", "message": run.error})
                await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300)})
            except asyncio.CancelledError:
                # Two very different cancellations land here:
                #  * stop_worker() cancelled THIS worker — the coordinator keeps
                #    going with the rest, so report it as stopped and carry on;
                #  * the COORDINATOR was cancelled (the user pressed Stop) and
                #    the cancellation propagated into the worker we are running.
                #    Swallowing that one made the sequential loop resume and
                #    launch the next worker — and the reviewer — after Stop.
                # `run.stop_requested` (set by stop_worker) tells them apart.
                run.stopped_by_user = True
                run.error = None
                run.stop_reason = run.stop_reason_requested or "stopped"
                run.finished = run.finished or time.time()
                if not run.stop_requested:
                    raise
                await emit({"event": "done", **run.report(), "final_text": _short(run.text, 300) or "(stopped by the user)"})
            finally:
                run.finished = run.finished or time.time()
                if dog is not None and not dog.done():
                    dog.cancel()
                # Its files are free again: a dependent task later in a
                # sequential run (or a fixer after verification) may edit
                # what this worker wrote — until now the locks outlived the
                # worker and the second task in "parallel: false" was refused.
                if locks is not None:
                    locks.release(run.id)
                # The transcript is saved HERE, after the stop reason is final
                # (stopped / stalled / timeout), not in _run_subagent's finally.
                _save_transcript(run, sm)
                if run.session_id:
                    _ACTIVE_WORKERS.pop(run.session_id, None)
                    _WORKER_RUNS.pop(run.session_id, None)

        def _launch(run: SubagentRun, max_rounds: Optional[int] = None) -> asyncio.Task:
            task = asyncio.create_task(one(run, max_rounds))
            # The child session id is assigned inside _run_subagent; register
            # the task under it as soon as it exists so the UI can stop it.
            # No fixed cap: a queued worker may wait far longer than 10 s.
            async def _register():
                while not task.done():
                    if run.session_id:
                        _ACTIVE_WORKERS[run.session_id] = task
                        _WORKER_RUNS[run.session_id] = run
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
                except asyncio.CancelledError:
                    # The coordinator was cancelled (Stop). CancelledError is a
                    # BaseException, so `except Exception` below already lets it
                    # through — this is here so a future refactor cannot quietly
                    # turn Stop back into "start the next worker".
                    raise
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
            reviewer_task: Dict[str, Any] = {"name": REVIEWER_NAME, "instruction": instruction,
                                             "model": args.get("reviewer_model") or "", "files": []}
            reviewer_slug = str(args.get("reviewer_agent") or "")
            if reviewer_slug:
                # Vetted in parse_delegation_args: only a `mode: reviewer`
                # definition reaches here, so the swap cannot quietly put a
                # worker that writes into the slot that bypasses the locks.
                reviewer_task["agent"] = reviewer_slug
                from src import agent_defs as _defs
                _defs.resolve_task(reviewer_task, workspace=workspace)
            reviewer = SubagentRun(len(runs), reviewer_task, role="reviewer")
            # The reviewer runs after everyone else, so nobody is still writing:
            # this is the ONE place that fact is true, and the one place the
            # bypass is granted.
            reviewer.bypass_locks = True
            refused = _attach_permissions([reviewer], ctx, workspace, roots)
            if refused:
                # Cannot happen while the reviewer sits at the same depth as
                # the workers that already derived. Said out loud rather than
                # dropped, because an unrestricted reviewer is the one thing
                # this slot must never quietly become.
                logger.warning("delegate_agents: reviewer permissions could not be derived: %s", refused)
            try:
                await _launch(reviewer, max_rounds=max(6, min(args["max_rounds"], 16)))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            runs.append(reviewer)
        report = _build_report_text(runs, workspace, locks)
        dropped = int(args.get("dropped_tasks") or 0)
        if dropped:
            report += (
                f"\n\nNOTE: {dropped} task(s) were NOT run — delegate_agents runs at most {MAX_SUBAGENTS} "
                f"tasks per call and the extra ones were dropped. Call delegate_agents again with the remaining "
                "tasks (or do them yourself); do not report them as done."
            )
        return {
            "output": report,
            "exit_code": 0 if not any(r.error for r in runs) else 1,
            "subagents": [r.report() for r in runs],
            "duration_s": round(time.time() - t0, 1),
            "lock_conflicts": list(locks.conflicts),
            "dropped_tasks": dropped,
        }
