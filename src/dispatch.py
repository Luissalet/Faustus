"""Dispatch: local workers for an outside coordinator (Fable / Claude / any
API caller), so the expensive model plans and reviews and never reads a tool
transcript.

    job = start(owner, {"tasks": [...], "workspace": "D:/proj", ...})
    ...
    compact(job)  →  a few hundred tokens: per task status, what changed,
                     the verification, the worker's last words; never the
                     transcript.

Each job runs the SAME machinery as `/agents` in a chat (delegate_agents:
src/agent_tools/subagent_tools.py — file locks, watchdog, supervisor, the
machine-wide GPU semaphore, lean toolset, the control board), inside a
"Workers" chat of its own so the human can open the board, steer or stop a
worker, and read the transcripts. The model is `dispatch_endpoint_id` /
`dispatch_model` from settings (falls back to the utility, then the default
chat model), or the request's `model` on that endpoint; the request never
names a URL.

What makes the answer trustworthy (none of it comes from the worker):
  * evidence   — the workspace is checkpointed before the job (the harness's
                 shadow repo) and diffed after it: `changes` is what really
                 changed on disk, `claimed_only` what a worker said it changed
                 but did not;
  * verification — Faustus runs the project's tests (or the `verify` command
                 the coordinator gave) in the workspace after the workers,
                 compares failures with the checkpoint (pre-existing vs new),
                 and, when they fail, sends ONE fixer worker with the failure
                 output before verifying again (`fix_rounds` — a MAXIMUM: the
                 loop also stops by itself when the rounds stop producing
                 change, see src/convergence.py);
  * proof       — the step after observing (src/prove.py): "a mutation is not
                 the completion of the objective". `result.proof` reconciles
                 what was observed with what was claimed and answers `proved`,
                 `partial`, `unproved` (no runner and nothing observable
                 changed — honest, not a failure) or `contradicted`, with a
                 confidence and a NAMED reason for every point it is missing;
  * honest status — `done` only when every worker finished and the
                 verification passed (or could not run); otherwise `partial`
                 with the reason in `verdict`; a cancelled job still reports
                 what changed.
Jobs in the same workspace run one at a time (a second one waits, `queued`);
a retried POST with the same `Idempotency-Key` returns the first job.

A SEQUENTIAL job whose tasks name objectives (`OBJ-3`) runs them in the order
the project's objectives graph ranks those objectives, not the order they were
typed (`order_tasks_by_impact`, `agent_objective_ordering`). That is the whole
of it: ONE job's task list, recorded in `task_order` and said in the verdict.
There is no queue across jobs and no scheduler here.

Jobs live in memory with a JSON mirror under DATA_DIR/dispatch/ (rotated at
MAX_JOBS_KEPT) so a finished job can still be read after a restart (a
running one is reported as `interrupted`).

Waiting is a CONDITION, not a sleep (`wait_for`): a caller blocks until the
job is done, reaches a phase, a worker enters a state read off its own output
(src/output_rules.py), an event says something, or the workspace changes on
disk — and it resolves the moment the condition holds, because the job's own
progress updates set the `asyncio.Event` the waiter sleeps on. There is no
poll tick inside. A timeout is not an error: it answers `met: False` with how
long it waited, the way the four-value outcomes in this tree already do.
Those same updates feed the live event stream
(`GET /api/dispatch/{id}/events?stream=1`, routes/dispatch_routes.py).

A worker detected `rate_limited` or `waiting_for_input` is REPORTED, never
killed: the state and the literal that proves it land in that worker's
`progress` entry while it runs, and the existing supervisor/ceiling logic
still owns every decision to stop anything.

A task may name a `runner` (src/agent_runners.py): an agent Faustus did not
write — Claude Code, OpenCode, Qwen Code, whatever `ollama launch` knows —
run as a worker through src/external_worker.py. Everything above still
happens around it: the checkpoint before, the diff after, `claimed_only`,
Faustus's own verification, the fix round, the proof. What does NOT happen is
the one thing this app is otherwise careful about: **Faustus's command guard
cannot see inside another agent's own shell**, so a job that used one carries
an explicit `external_agent_unguarded` entry in its proof's uncertainty list
and says so in its verdict. A task with no `runner` runs exactly as it always
has, byte for byte.
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
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_TASKS = 4                 # delegate_agents' own cap (MAX_SUBAGENTS)
MAX_JOBS_KEPT = 200
SUMMARY_CHARS = 1200          # the worker's last words, per task
EVENTS_KEPT = 400
CHANGES_LISTED = 60           # paths listed per change kind in the compact result
_DEFAULT_TIMEOUT_S = 900
_DEFAULT_MAX_ROUNDS = 20
_DEFAULT_FIX_ROUNDS = 1
_MAX_FIX_ROUNDS = 2
# With the convergence detector on, the fix loop ends itself as soon as the
# rounds stop producing change, so a caller may ask for more of them without
# buying a fixed number of pointless workers.
_MAX_FIX_ROUNDS_CONVERGENCE = 4
_VERIFY_TIMEOUT_S = 300
_VERIFY_CMD_CHARS = 500
_IDEMPOTENCY_TTL_S = 3600
_LIVE = ("queued", "running", "verifying", "cancelling")
# How much of each worker's own output is kept for the state rules. The rules
# only read their own tail (src/output_rules.py); this bounds what a job holds.
_OUTPUT_TAIL_CHARS = 8192
# The event keys that carry a worker's OWN words (its live command tail, a
# tool's output, its last words, its error) — the only text the rules read.
_OUTPUT_KEYS = ("tail", "output", "final_text", "message")
# A task run by an agent Faustus did not write (src/external_worker.py): the
# named reason that goes into the proof's uncertainty list, and the sentence it
# carries. The proof exists to name every reason its confidence is not 1; an
# unguarded external shell is the biggest one this app can have.
EXTERNAL_UNGUARDED = "external_agent_unguarded"
EXTERNAL_UNGUARDED_DETAIL = ("an external agent ran its own shell; Faustus's command guard did not "
                             "see its commands")
# What that entry costs the proof's confidence. The same weight src/prove.py
# gives an uncertainty it has no penalty for, so re-sorting the list by
# `prove.PENALTY` keeps it exactly where prove itself would have put it.
EXTERNAL_UNGUARDED_PENALTY = 0.1
# How much of an external agent's output is kept as its "last words".
EXTERNAL_SUMMARY_CHARS = 2000

# `wait_for(condition=...)`: the prefixes a condition may take.
WAIT_CONDITIONS = ("done", "changed", "phase:<name>", "worker:<label>:<state>", "event:<text>")
# `changed` re-reads the workspace on every job update; two scans closer
# together than this reuse the previous answer (a flooding worker must not
# turn one wait into a tree walk per line).
_CHANGED_SCAN_MIN_INTERVAL_S = 0.25
# Live events (SSE): a comment every STREAM_HEARTBEAT_S so no proxy closes an
# idle stream, and the stream itself never outlives the job's own ceiling by
# more than STREAM_MARGIN_S.
STREAM_HEARTBEAT_S = 15.0
STREAM_MARGIN_S = 120.0
_SNAPSHOT_MAX_FILES = 60_000
_SNAPSHOT_SKIP = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".mypy_cache", ".pytest_cache",
                            ".ruff_cache", ".tox", "dist", "build", ".idea", ".vscode", "target", ".next", ".cache",
                            ".odysseus_checkpoints", ".faustus"})

_jobs: Dict[str, "DispatchJob"] = {}
_lock = asyncio.Lock()
_idempotent: Dict[Tuple[str, str], Tuple[str, float]] = {}   # (owner, key) → (job id, ts)
_loaded_all_at = 0.0


def _data_dir() -> str:
    try:
        from src.constants import DATA_DIR
        return os.path.join(DATA_DIR, "dispatch")
    except Exception:  # pragma: no cover
        return os.path.join(os.getcwd(), "data", "dispatch")


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - a job never fails over a settings read
        return default


def _convergence_on() -> bool:
    """`agent_fix_round_convergence`. Off = the fixed fix-round counter."""
    return bool(_setting("agent_fix_round_convergence", True))


def state_detection_on() -> bool:
    """`agent_worker_state_detection`. Off = a worker's output is never read
    for a state and `progress` carries exactly what it carried before."""
    return bool(_setting("agent_worker_state_detection", True))


def sse_on() -> bool:
    """`agent_dispatch_sse`. Off = `/{id}/events?stream=1` answers with the
    same JSON body the endpoint has always returned."""
    return bool(_setting("agent_dispatch_sse", True))


def prove_on() -> bool:
    """`agent_dispatch_prove`. Off = the result payload and the verdict line
    are exactly what they were before src/prove.py existed."""
    try:
        from src import prove
        return prove.enabled()
    except Exception:  # noqa: BLE001
        return False


def objective_ordering_on() -> bool:
    """`agent_objective_ordering`. Off = the tasks of a job run in the order
    they were written, which is what they have always done."""
    return bool(_setting("agent_objective_ordering", True))


def _outcomes_on() -> bool:
    """`agent_tool_outcomes`. Off = a stopped worker counts as an error."""
    try:
        from src import tool_outcome
        return tool_outcome.enabled()
    except Exception:  # noqa: BLE001
        return False


def external_runners_on() -> bool:
    """`agent_external_runners`. **Off by default** — it runs third-party
    binaries on this machine. Off = a task naming a `runner` is refused with
    the reason, and a job without one is untouched."""
    try:
        from src import agent_runners
        return agent_runners.enabled()
    except Exception:  # noqa: BLE001 - never raise into a hot path
        return False


def _max_fix_rounds() -> int:
    return _MAX_FIX_ROUNDS_CONVERGENCE if _convergence_on() else _MAX_FIX_ROUNDS


def _timing_key(workspace: Optional[str]) -> str:
    """The adaptive-timeout bucket a job's duration is remembered under: one
    per workspace, because how long a job takes is a property of the project."""
    return "dispatch:" + (_ws_key(workspace) or "-")


def _adaptive_ceiling(workspace: Optional[str], fixed: int) -> int:
    """The fixed ceiling, raised to what jobs in this workspace really take."""
    try:
        from src import adaptive_timeout as at
        if not at.enabled():
            return fixed
        key = _timing_key(workspace)
        value = at.idle_timeout(key, fixed, lo=fixed, hi=max(fixed, fixed * 3))
        if value > fixed:
            at.note_difference(key, value, fixed, what="job ceiling")
            return int(value)
    except Exception as e:  # noqa: BLE001 - a poller hint, never load-bearing
        logger.debug("dispatch: adaptive ceiling unavailable: %s", e)
    return fixed


def _record_job_duration(job: "DispatchJob") -> None:
    """Feed this job's wall-clock into the adaptive recorder (best effort)."""
    try:
        from src import adaptive_timeout as at
        if not at.enabled() or not job.started or not job.finished:
            return
        at.record(_timing_key(job.workspace), float(job.finished) - float(job.started))
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch: could not record the job duration: %s", e)


def _round_artifact(job: "DispatchJob", v: Dict[str, Any]) -> str:
    """What one fix round LEFT BEHIND, as the convergence detector reads it:
    the verification's verdict and failures, the tail of its output and the
    files that changed on disk. Two rounds that leave the same artifact
    changed nothing between them."""
    changes = job.changes or {}
    touched = sorted(list(changes.get("added") or []) + list(changes.get("modified") or [])
                     + list(changes.get("deleted") or []))[:CHANGES_LISTED]
    parts = [
        str(v.get("summary") or ""),
        "\n".join(str(f) for f in (v.get("failures") or [])[:20]),
        str(v.get("output_tail") or "")[-2000:],
        "changed: " + ", ".join(touched),
    ]
    return "\n".join(p for p in parts if p.strip())


def _assess_convergence(rounds: List[str]) -> Optional[Dict[str, Any]]:
    try:
        from src import convergence
        return convergence.assess(rounds)
    except Exception as e:  # noqa: BLE001 - the fix loop runs without it
        logger.debug("dispatch: convergence unavailable: %s", e)
        return None


class DispatchJob:
    def __init__(self, owner: Optional[str], args: Dict[str, Any], workspace: Optional[str],
                 endpoint_url: str, model: str, headers: Optional[Dict[str, str]],
                 title: str, gen_overrides: Optional[Dict[str, Any]] = None,
                 verify: str = "auto", verify_scope: str = "related", fix_rounds: int = _DEFAULT_FIX_ROUNDS,
                 verify_timeout_s: float = _VERIFY_TIMEOUT_S):
        self.id = uuid.uuid4().hex[:12]
        self.owner = owner
        self.args = args
        self.workspace = workspace
        self.endpoint_url = endpoint_url
        self.model = model
        self.headers = headers
        self.title = title
        self.gen_overrides = gen_overrides
        self.verify = verify                    # "auto" | "none" | a shell command
        self.verify_scope = verify_scope        # "related" | "all"
        self.fix_rounds = fix_rounds
        self.verify_timeout_s = verify_timeout_s
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        # queued | running | verifying | done | partial | error | cancelling | cancelled | interrupted
        self.status = "queued"
        self.error: Optional[str] = None
        self.verdict: Optional[str] = None
        self.session_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.changes: Optional[Dict[str, Any]] = None        # observed by Faustus, not claimed by a worker
        self.verification: Optional[Dict[str, Any]] = None
        self.checkpoint: Optional[str] = None
        # Convergence of the fix loop (src/convergence.py) and, when the loop
        # ended for a reason other than "the rounds ran out", which one.
        self.convergence: Optional[Dict[str, Any]] = None
        self.stopped_by: Optional[str] = None
        # What the objectives graph did to the task list before the job ran
        # ({"by": "impact", "from": [...], "to": [...]}), so the reordering is
        # visible and auditable instead of silent. None when nothing moved and
        # while `agent_objective_ordering` is off.
        self.task_order: Optional[Dict[str, Any]] = None
        # The proof packet (src/prove.py): what the evidence and the
        # verification really SHOW, with every reason the confidence is not 1
        # named. None while `agent_dispatch_prove` is off.
        self.proof: Optional[Dict[str, Any]] = None
        # The external agents this job ran, if any (src/agent_runners.py). An
        # empty list is the normal case and adds NOTHING to the payload: a job
        # with no runner is byte-identical to one from before this existed.
        self.runners_used: List[str] = []
        self.events: Deque[Dict[str, Any]] = deque(maxlen=EVENTS_KEPT)
        self.task: Optional[asyncio.Task] = None
        self._waiters: List[asyncio.Event] = []
        self._entered = False                 # _run has begun (its finally will settle the job)
        # What each worker's own output says about it (src/output_rules.py):
        # name → {state, why, matched, confidence, seen: [...]}. Never a
        # reason to kill anything — it is what `progress` reports.
        self.worker_states: Dict[str, Dict[str, Any]] = {}
        self._output: Dict[str, str] = {}      # name → the tail of its output
        # Every event ever appended (the deque rotates at EVENTS_KEPT): a
        # stream that has sent N of them knows what is new without stamping
        # a sequence number onto the events themselves.
        self.events_produced = 0
        self._updates: List[asyncio.Event] = []   # woken on every change (wait_for, SSE)

    # ── views ────────────────────────────────────────────────────────────

    def to_dict(self, *, include_result: bool = True, brief: bool = False) -> Dict[str, Any]:
        d = {
            "id": self.id, "owner": self.owner, "title": self.title, "status": self.status,
            "error": self.error, "verdict": self.verdict, "workspace": self.workspace, "model": self.model,
            "session_id": self.session_id, "chat_url": f"/#{self.session_id}" if self.session_id else None,
            "created": self.created, "started": self.started, "finished": self.finished,
            "duration_s": round((self.finished or time.time()) - (self.started or self.created), 1),
            "tasks": [{"name": t.get("name"), "instruction": _squash(t.get("instruction"), 200) if brief else t.get("instruction"),
                       "files": t.get("files") or [], "model": t.get("model") or None}
                      for t in self.args.get("tasks") or []],
            "parallel": bool(self.args.get("parallel")), "reviewer": bool(self.args.get("reviewer")),
            "max_rounds": self.args.get("max_rounds"), "timeout_s": self.args.get("timeout_s"),
            "verify": self.verify, "verify_scope": self.verify_scope, "fix_rounds": self.fix_rounds,
        }
        if self.task_order is not None:
            d["task_order"] = self.task_order
        if include_result:
            d["result"] = self.result
            d["changes"] = self.changes
            d["verification"] = self.verification
            d["checkpoint"] = self.checkpoint
            if self.convergence is not None:
                d["convergence"] = self.convergence
            if self.stopped_by:
                d["stopped_by"] = self.stopped_by
            if self.proof is not None:
                d["proof"] = self.proof
        if self.runners_used:
            # Only ever present when an external agent really ran: what the
            # command guard could not see has to be readable in the payload,
            # not only in the proof.
            d["runners"] = list(self.runners_used)
            d["unguarded"] = True
        return d

    def ceiling_s(self) -> int:
        """The most wall-clock the job can take: every worker's timeout in
        turn at the configured parallelism, a reviewer, the verification and
        the fix loop — so a coordinator knows how long to keep waiting.

        With `agent_adaptive_idle_timeout` on, jobs that really took longer
        than this estimate in this workspace raise it (never lower it: a
        coordinator that stops waiting early re-dispatches work that is still
        running)."""
        tasks = len(self.args.get("tasks") or [])
        try:
            from src.agent_tools.subagent_tools import _setting as _sa_setting
            par = max(1, int(_sa_setting("agent_subagent_max_parallel", 2) or 1))
        except Exception:
            par = 2
        per = int(self.args.get("timeout_s") or _DEFAULT_TIMEOUT_S)
        waves = -(-tasks // par) if self.args.get("parallel") else tasks
        n = per * max(1, waves) + (per if self.args.get("reviewer") else 0)
        if self.verify != "none":
            n += int(self.verify_timeout_s) * (1 + self.fix_rounds) + per * self.fix_rounds
        return int(_adaptive_ceiling(self.workspace, n))

    def _notify(self) -> None:
        for ev in self._waiters:
            ev.set()
        self._waiters.clear()
        self._wake()

    # ── live updates: what makes a wait resolve at once ──────────────────

    def _wake(self) -> None:
        """Wake every condition waiter and every open stream. Called from the
        job's own progress path, so a `wait_for` resolves on the update that
        made its condition true instead of on a poll tick."""
        try:
            for ev in list(self._updates):
                ev.set()
        except Exception as e:  # noqa: BLE001 - a notification never breaks a job
            logger.debug("dispatch %s: wake failed: %s", self.id, e)

    def subscribe(self) -> asyncio.Event:
        """An Event set on every change to this job. The caller MUST call
        :meth:`unsubscribe` in a finally (a client that disconnects mid-stream
        must not leave a waiter behind)."""
        ev = asyncio.Event()
        self._updates.append(ev)
        return ev

    def unsubscribe(self, ev: asyncio.Event) -> None:
        try:
            self._updates.remove(ev)
        except ValueError:
            pass

    def note_worker_event(self, ev: Dict[str, Any]) -> None:
        """One board event from a worker: kept as it is (the events endpoint
        answers exactly what it always did), read for a state, and broadcast."""
        self.events.append(ev)
        self.events_produced += 1
        try:
            self._detect(ev)
        except Exception as e:  # noqa: BLE001 - the rules never break a job
            logger.debug("dispatch %s: state detection failed: %s", self.id, e)
        self._wake()

    def _detect(self, ev: Dict[str, Any]) -> None:
        """Classify the newest words of one worker (`agent_worker_state_detection`).

        A `rate_limited` or `waiting_for_input` worker is RECORDED here and
        surfaced in `progress`; nothing in this path stops a worker — that
        stays with the supervisor and the job ceiling.
        """
        if not state_detection_on():
            return
        name = str(ev.get("name") or "").strip()
        if not name or name == "job":
            return
        chunk = "\n".join(str(ev.get(k)) for k in _OUTPUT_KEYS if ev.get(k)).strip()
        if not chunk:
            return
        from src import output_rules
        buf = (self._output.get(name, "") + "\n" + chunk)[-_OUTPUT_TAIL_CHARS:]
        self._output[name] = buf
        verdict = output_rules.classify_output(buf)
        entry = self.worker_states.setdefault(name, {"seen": []})
        states = list(verdict.get("states") or [])
        if not states:
            for key in ("state", "why", "matched", "confidence"):
                entry.pop(key, None)
            return
        matches = verdict.get("matches") or []
        entry["state"] = states[0]
        entry["states"] = states
        entry["why"] = output_rules.why(verdict, states[0])
        entry["matched"] = str((matches[0] or {}).get("literal") or "") if matches else ""
        entry["confidence"] = verdict.get("confidence")
        entry["ts"] = time.time()
        for s in states:
            if s not in entry["seen"]:
                entry["seen"].append(s)

    def _persist(self) -> None:
        try:
            d = _data_dir()
            os.makedirs(d, exist_ok=True)
            tmp = os.path.join(d, f".{self.id}.tmp")
            # The pid goes in the MIRROR only, never in the API payload: it is
            # what src/crash_recovery.py probes before calling a job that was
            # left `running` interrupted. A pid that still answers means the
            # job belongs to a process that is alive, not to a power cut.
            doc = dict(self.to_dict(), pid=os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(d, f"{self.id}.json"))
        except Exception as e:  # noqa: BLE001 — a mirror, never load-bearing
            logger.debug("dispatch: persist failed: %s", e)

    def _event(self, **ev: Any) -> None:
        ev.setdefault("ts", time.time())
        ev.setdefault("name", "job")
        self.events.append(ev)
        self.events_produced += 1
        self._wake()


# ── the compact answer ──────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def _squash(text: Any, limit: int) -> str:
    s = _WS_RE.sub(" ", str(text or "")).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _compact_static_checks(sc: Any) -> Any:
    """Only the failures, bounded — a list of 40 `ok: True` rows per worker is
    what turned the "few hundred tokens" answer into 14k."""
    if isinstance(sc, list):
        bad = [x for x in sc if isinstance(x, dict) and x.get("ok") is False]
        return {"checked": len(sc), "failed": [{"path": str(x.get("path"))[:120], "error": _squash(x.get("error"), 200)}
                                              for x in bad[:10]]}
    if isinstance(sc, dict):
        return {k: (_squash(v, 200) if isinstance(v, str) else v) for k, v in list(sc.items())[:8]}
    return _squash(sc, 200)


def compact_from_result(result: Optional[Dict[str, Any]], *, summary_chars: int = SUMMARY_CHARS) -> Dict[str, Any]:
    """What an outside coordinator needs and nothing more: per worker the
    status, the files it says it changed, its tool/round/token counts, the
    static-check failures and its last words — never the transcript. The
    per-worker `git` snapshot is dropped: it was the WHOLE tree's status
    repeated once per worker; the job's `changes` block is the honest one."""
    out: Dict[str, Any] = {"workers": [], "files_changed": [], "totals": {
        "tool_calls": 0, "failed_calls": 0, "rounds": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}}
    if not isinstance(result, dict):
        return out
    outcomes_on = _outcomes_on()
    cancelled = 0
    changed: List[str] = []
    for r in result.get("subagents") or []:
        if not isinstance(r, dict):
            continue
        w = {
            "name": r.get("name"), "role": r.get("role") or "worker", "status": r.get("status"),
            "stop_reason": r.get("stop_reason"), "error": _squash(r.get("error"), 300) or None,
            "rounds": int(r.get("rounds") or 0), "tool_calls": int(r.get("tool_calls") or 0),
            "failed_calls": int(r.get("failed_calls") or 0),
            "files_changed": [str(p) for p in list(r.get("mutations") or [])[:40]],
            "input_tokens": int(r.get("input_tokens") or 0), "output_tokens": int(r.get("output_tokens") or 0),
            "duration_s": r.get("duration_s"), "model": r.get("model"),
            "summary": _squash(r.get("final_text"), summary_chars),
            "session_id": r.get("session_id"),
        }
        if outcomes_on:
            w["outcome"] = _worker_outcome(r)
        sc = r.get("static_checks")
        if sc:
            w["static_checks"] = _compact_static_checks(sc)
        if r.get("supervisor"):
            w["supervisor"] = [_squash(x, 160) for x in list(r.get("supervisor") or [])[:4]]
        if r.get("runner"):
            # An external agent (src/external_worker.py). These keys exist ONLY
            # on such a row: a built-in worker's row is what it always was.
            w["runner"] = str(r.get("runner"))
            w["unguarded"] = True
            if r.get("argv_shown"):
                w["argv_shown"] = _squash(r.get("argv_shown"), 400)
            if r.get("state"):
                w["state"] = str(r.get("state"))
                if r.get("why"):
                    w["why"] = _squash(r.get("why"), 200)
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
        # any worker that did not finish its task counts — stalled, stopped
        # and timed-out workers have no `error` and used to be 0 errors.
        # A worker the USER stopped is the exception (four-value outcomes): it
        # is `cancelled`, counted apart, and never blamed on the model.
        if outcomes_on and w.get("outcome") == "cancelled":
            cancelled += 1
        elif w["error"] or (w["status"] not in ("done", None)):
            t["errors"] += 1
    if cancelled:
        out["totals"]["cancelled"] = cancelled
    out["files_changed"] = changed
    if result.get("lock_conflicts"):
        out["lock_conflicts"] = [f"{c.get('worker')} → {c.get('path')}" for c in list(result["lock_conflicts"])[:10]
                                 if isinstance(c, dict)]
    if result.get("dropped_tasks"):
        out["dropped_tasks"] = int(result["dropped_tasks"])
    out["exit_code"] = result.get("exit_code")
    return out


def _worker_outcome(r: Dict[str, Any]) -> Optional[str]:
    """The four-value outcome of one worker report: the one the worker itself
    recorded when it is there, else read from its status and error."""
    try:
        from src import tool_outcome
        known = tool_outcome.value_of(r.get("outcome"))
        if known:
            return known
        return tool_outcome.classify_status(r.get("status") or r.get("stop_reason"),
                                            error=r.get("error")).value
    except Exception:  # noqa: BLE001 - the compact answer never fails over this
        return None


def _seed_progress(job: DispatchJob) -> Dict[str, Dict[str, Any]]:
    """Every task appears in `progress` from the start (`queued`), so a
    4-task job at max_parallel 2 never looks like a 2-worker job."""
    latest: Dict[str, Dict[str, Any]] = {}
    for i, t in enumerate(job.args.get("tasks") or []):
        name = str(t.get("name") or f"worker-{i + 1}")
        latest[name] = {"last_event": "queued"}
    return latest


def compact(job: DispatchJob) -> Dict[str, Any]:
    d = job.to_dict(include_result=False)
    res = compact_from_result(job.result)
    if job.changes is not None:
        # what Faustus SAW on disk beats what a worker SAID it wrote
        observed = list(job.changes.get("added") or []) + list(job.changes.get("modified") or []) + list(job.changes.get("deleted") or [])
        claimed = list(res["files_changed"])
        res["files_changed"] = observed[: CHANGES_LISTED * 3]
        res["claimed_only"] = [p for p in claimed if not _observed(p, job.changes)][:20]
        res["changes"] = job.changes
    if job.verification is not None:
        res["verification"] = job.verification
    if job.convergence is not None:
        res["convergence"] = job.convergence
    if job.stopped_by:
        res["stopped_by"] = job.stopped_by
    # The proof of what the job really did (src/prove.py). Absent while
    # `agent_dispatch_prove` is off, so the payload stays byte-for-byte the one
    # a coordinator has been reading.
    if job.proof is not None:
        res["proof"] = job.proof
    if job.status not in _LIVE:
        res["exit_code"] = 0 if job.status == "done" else 1
    d["result"] = res
    # a running job: the board's latest tick per worker, so a poller sees
    # progress without the event stream
    if job.status in _LIVE:
        latest = _seed_progress(job)
        for ev in job.events:
            name = str(ev.get("name") or ev.get("id") or "")
            if not name or name == "job":
                continue
            kind = ev.get("event")
            if kind in ("queued", "started", "round", "tool", "tick", "steer", "supervisor", "done", "error", "guard", "harness"):
                cur = latest.setdefault(name, {})
                cur["last_event"] = kind
                for k in ("round", "elapsed_s", "idle_s", "last_tool", "tool", "stalled", "stall_reason", "status"):
                    if k in ev:
                        cur[k] = ev[k]
        # What the worker's own output says about it, while it runs rather
        # than after (`agent_worker_state_detection`; off = nothing is added).
        for name, st in (job.worker_states or {}).items():
            if not st.get("state") or name not in latest:
                continue
            latest[name]["state"] = st["state"]
            if st.get("why"):
                latest[name]["why"] = st["why"]
        d["progress"] = latest
        d["wait_again"] = True
        d["ceiling_s"] = job.ceiling_s()
        for ev in reversed(job.events):
            if ev.get("name") == "job" and ev.get("message"):
                d["phase"] = ev["message"]
                break
    return d


def _observed(path: str, changes: Dict[str, Any]) -> bool:
    p = str(path or "").replace("\\", "/").strip("/").lower()
    for kind in ("added", "modified", "deleted"):
        for q in changes.get(kind) or []:
            qq = str(q).replace("\\", "/").strip("/").lower()
            if qq == p or qq.endswith("/" + p) or p.endswith("/" + qq):
                return True
    return bool(changes.get("truncated"))


# ── evidence: what changed on disk ──────────────────────────────────────────

def _snapshot(workspace: str) -> Tuple[Dict[str, Tuple[int, int]], bool]:
    """rel path → (mtime_ns, size) for the workspace tree (bounded, skips the
    usual generated folders). The fallback when the harness's checkpoints are
    unavailable (no git on the box)."""
    files: Dict[str, Tuple[int, int]] = {}
    truncated = False
    root = os.path.realpath(workspace)
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _SNAPSHOT_SKIP:
                                stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            rel = os.path.relpath(entry.path, root).replace("\\", "/")
                            files[rel] = (int(st.st_mtime_ns), int(st.st_size))
                            if len(files) >= _SNAPSHOT_MAX_FILES:
                                return files, True
                    except OSError:
                        continue
        except OSError:
            continue
    return files, truncated


def _diff_snapshots(before: Dict[str, Tuple[int, int]], after: Dict[str, Tuple[int, int]], truncated: bool) -> Dict[str, Any]:
    added = sorted(p for p in after if p not in before)
    deleted = sorted(p for p in before if p not in after)
    modified = sorted(p for p in after if p in before and after[p] != before[p])
    return _changes_block(added, modified, deleted, "mtime", truncated)


def _changes_block(added: List[str], modified: List[str], deleted: List[str], source: str, truncated: bool = False) -> Dict[str, Any]:
    n = len(added) + len(modified) + len(deleted)
    return {"source": source, "count": n, "added": added[:CHANGES_LISTED], "modified": modified[:CHANGES_LISTED],
            "deleted": deleted[:CHANGES_LISTED],
            "truncated": bool(truncated or n > CHANGES_LISTED * 3 or max(len(added), len(modified), len(deleted)) > CHANGES_LISTED)}


def _checkpoint(workspace: str, label: str) -> Optional[str]:
    try:
        from src import workspace_checkpoints as wc
        if not wc.enabled():
            return None
        cp = wc.checkpoint(workspace, label=label)
        return str(cp.get("sha")) if cp and cp.get("sha") else None
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch: checkpoint failed: %s", e)
        return None


def _changes_since(workspace: str, sha: str) -> Optional[Dict[str, Any]]:
    try:
        from src import workspace_checkpoints as wc
        rows = wc.changed_since(workspace, sha)
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch: changed_since failed: %s", e)
        return None
    if rows is None:
        return None
    added = sorted(r["path"] for r in rows if r.get("status") == "A")
    deleted = sorted(r["path"] for r in rows if r.get("status") == "D")
    modified = sorted(r["path"] for r in rows if r.get("status") not in ("A", "D"))
    block = _changes_block(added, modified, deleted, "checkpoint")
    block["checkpoint"] = sha[:12]
    return block


def _git_facts(workspace: str) -> Optional[Dict[str, Any]]:
    """The user's own repo, once per job: is it one, how dirty is it now."""
    try:
        from src.agent_harness import git_change_summary
        g = git_change_summary(workspace)
    except Exception:
        return None
    if not g:
        return None
    return {"repo": True, "dirty_count": int(g.get("changed_count") or 0), "shortstat": _squash(g.get("shortstat"), 200)}


class _Evidence:
    """Before/after the job: a checkpoint (content-exact, via the harness's
    shadow repo) or an mtime snapshot of the tree."""

    def __init__(self, workspace: Optional[str], job_id: str):
        self.workspace = workspace
        self.sha: Optional[str] = None
        self.snap: Optional[Dict[str, Tuple[int, int]]] = None
        self.snap_truncated = False
        self.label = f"dispatch {job_id}"

    def before(self) -> None:
        if not self.workspace:
            return
        self.sha = _checkpoint(self.workspace, self.label)
        if not self.sha:
            self.snap, self.snap_truncated = _snapshot(self.workspace)

    def after(self) -> Optional[Dict[str, Any]]:
        if not self.workspace:
            return None
        block = _changes_since(self.workspace, self.sha) if self.sha else None
        if block is None and self.snap is not None:
            now, trunc = _snapshot(self.workspace)
            block = _diff_snapshots(self.snap, now, self.snap_truncated or trunc)
        if block is not None:
            git = _git_facts(self.workspace)
            if git:
                block["git"] = git
        return block


# ── verification: Faustus runs the proof, not the worker ────────────────────

def _verification_spec(workspace: str, verify: str) -> Tuple[Optional[Dict[str, Any]], str]:
    from src import project_tests as pt
    if verify == "none":
        return None, "off"
    if verify and verify != "auto":
        return pt.detect_test_command(workspace, override=verify), "command"
    override = ""
    try:
        from src.settings import get_setting
        override = str(get_setting("agent_project_test_command", "") or "").strip()
    except Exception:
        override = ""
    return pt.detect_test_command(workspace, override=override or None), "auto"


def run_verification(workspace: Optional[str], verify: str, changed: List[str], *, scope: str = "related",
                     timeout_s: float = _VERIFY_TIMEOUT_S, checkpoint_sha: Optional[str] = None) -> Dict[str, Any]:
    """Run the project's tests (or `verify`) in the workspace, bounded, and
    return a compact verdict. `ok` is None when nothing could be run — that is
    "not verified", never "passed"."""
    if not workspace:
        return {"mode": "off", "ran": False, "ok": None, "summary": "no workspace"}
    try:
        spec, mode = _verification_spec(workspace, verify)
    except Exception as e:  # noqa: BLE001
        return {"mode": "auto", "ran": False, "ok": None, "summary": f"verification unavailable: {e}"[:200]}
    if mode == "off":
        return {"mode": "off", "ran": False, "ok": None, "summary": "verification disabled by the request (verify: none)"}
    if not spec:
        return {"mode": mode, "ran": False, "ok": None,
                "summary": "no test runner detected in the workspace (give `verify` a command that proves the task)"}
    from src import project_tests as pt
    res = pt.run_tests(workspace, spec, changed=list(changed or []), scope=scope, timeout_s=timeout_s)
    if res.get("ran") and res.get("ok") is False and not res.get("inconclusive") and checkpoint_sha:
        try:
            res = pt.compare_with_baseline(workspace, checkpoint_sha, spec, res, changed=list(changed or []))
        except Exception as e:  # noqa: BLE001
            logger.debug("dispatch: baseline comparison failed: %s", e)
    out = {
        "mode": mode, "ran": bool(res.get("ran")), "ok": res.get("ok"), "inconclusive": bool(res.get("inconclusive")),
        "kind": res.get("kind"), "command": _squash(res.get("command"), 300), "scope": res.get("scope"),
        "exit_code": res.get("exit_code"), "timed_out": bool(res.get("timed_out")), "duration_s": res.get("duration_s"),
        "summary": _squash(res.get("summary"), 300), "failures": [_squash(f, 200) for f in (res.get("failures") or [])[:10]],
        "output_tail": str(res.get("output_tail") or "")[-1500:],
    }
    if res.get("related_files"):
        out["related_files"] = [str(p) for p in res["related_files"][:12]]
    for k in ("new_failures", "pre_existing"):
        if res.get(k):
            out[k] = [_squash(f, 200) for f in res[k][:10]]
    if res.get("pre_existing_only"):
        out["pre_existing_only"] = True
    return out


def verification_failed(v: Optional[Dict[str, Any]]) -> bool:
    """A verdict that should block `done`: it ran, it failed, it is
    conclusive, and the failures are not all pre-existing."""
    return bool(v and v.get("ran") and v.get("ok") is False and not v.get("inconclusive") and not v.get("pre_existing_only"))


def _fixer_instruction(job: DispatchJob, v: Dict[str, Any], attempt: int) -> str:
    """What the fix round asks for — the same words whether or not the worker
    is resumed.

    It was tempting to drop the task recap for a resumed worker, since its own
    session already carries it. It is not safe: this side decides to resume
    OPTIMISTICALLY and the worker side is the one that finds out whether the
    session still has any history (it may have been pruned, or the manager may
    keep none). Trimming here would produce the one outcome worse than today's
    — a thin instruction AND no recovered context. Resume stays purely
    additive: same words, plus whatever the session still remembers.
    """
    lines = [
        f"[Verification failed after the workers' changes — fix round {attempt}]",
        f"The command `{v.get('command') or v.get('summary')}` FAILED in the workspace: {v.get('summary') or 'failed'}.",
    ]
    pre = set(v.get("pre_existing") or [])
    for f in (v.get("failures") or [])[:8]:
        lines.append(f"- {f}" + (" (already failed before the workers' change)" if f in pre else ""))
    tail = (v.get("output_tail") or "").strip()
    if tail:
        lines.append("Output (tail):")
        lines.append(tail[-2000:])
    lines.append("")
    tasks = "\n".join(f"{i}. {_squash(t.get('instruction'), 400)}" for i, t in enumerate(job.args.get("tasks") or [], 1))
    lines.append("The workers were asked to:")
    lines.append(tasks)
    if job.changes and job.changes.get("count"):
        touched = (job.changes.get("added") or []) + (job.changes.get("modified") or [])
        lines.append("Files they changed: " + ", ".join(touched[:30]))
    lines.append("")
    lines.append("Read the failing test and the code it exercises, fix the CAUSE (not the test, unless the test itself is "
                 "wrong per the task), run the same command yourself until it passes, then report in two sentences what "
                 "was wrong and what you changed. Do not start unrelated work.")
    return "\n".join(lines)


def resume_enabled() -> bool:
    """`agent_fixer_resume`. On by default: rebuilding a worker from the task
    plus the failure text makes it re-read the same files and rebuild the same
    model of the problem, which is the expensive half of a fix round and the
    reason `fix_rounds` is capped at 2."""
    return bool(_setting("agent_fixer_resume", True))


def _resume_target(job: DispatchJob, v: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The worker a fix round should CONTINUE, or None for today's fresh one.

    Preference, in order: the worker that touched a file the verification is
    complaining about, then any worker that changed something, then the last
    one that ran at all. Later beats earlier throughout, because the most
    recent session is the one holding the most recent state.

    Returns ``{"kind", "id", "runner", "name", "model"}`` — the handle plus
    what the fixer needs to be the same worker. None whenever nothing is
    resumable, which is every job that ran before this existed.
    """
    rows = [r for r in (job.result or {}).get("subagents") or [] if isinstance(r, dict)]
    related = {str(p) for p in ((v or {}).get("related_files") or [])}
    best: Optional[Dict[str, Any]] = None
    best_rank = -1
    for row in rows:
        if row.get("role") in ("reviewer", "fixer"):
            continue
        handle = str(row.get("runner_session") or "")
        kind = "runner" if handle else "session"
        if not handle:
            handle = str(row.get("session_id") or "")
        if not handle:
            continue
        mutations = [str(m) for m in (row.get("mutations") or [])]
        rank = 0
        if mutations:
            rank = 2 if (related and any(m in related for m in mutations)) else 1
        if rank >= best_rank:                 # >= so the LAST of equal rank wins
            best_rank = rank
            best = {"kind": kind, "id": handle, "runner": str(row.get("runner") or ""),
                    "name": str(row.get("name") or ""), "model": str(row.get("model") or ""),
                    # The definition the resumed worker ran under travels with
                    # the handle. Continuing a restricted worker's session as
                    # an unrestricted fixer would launder exactly the
                    # restriction the definition exists to hold.
                    "agent": str(row.get("agent") or "")}
    return best


# ── running a job ───────────────────────────────────────────────────────────

def _parse_tasks(raw: Any) -> List[Dict[str, Any]]:
    from src.agent_tools.subagent_tools import parse_delegation_args
    args = parse_delegation_args(json.dumps({"tasks": raw}) if not isinstance(raw, str) else raw)
    return list(args.get("tasks") or [])


def build_args(body: Dict[str, Any]) -> Dict[str, Any]:
    """The delegate_agents payload for a dispatch request (validated by the
    tool's own parser so a job and a chat delegation cannot disagree).

    `agent` / `reviewer_agent` are resolved HERE as well as inside the tool,
    because a definition may name a `runner` and `vet_runners` has to refuse a
    job whose agent is not installed BEFORE any worker spends its time. The
    resolution is idempotent, so the second pass inside the tool is free.
    """
    from src.agent_tools.subagent_tools import parse_delegation_args
    payload: Dict[str, Any] = {"tasks": body.get("tasks")}
    for k in ("parallel", "reviewer", "max_rounds", "timeout_s", "reviewer_model",
              "agent", "reviewer_agent"):
        if body.get(k) is not None:
            payload[k] = body[k]
    if body.get("context"):
        payload["context"] = str(body["context"])[:8000]
    args = parse_delegation_args(json.dumps(payload), workspace=str(body.get("workspace") or "") or None)
    if not args.get("tasks"):
        raise ValueError("tasks is required: a list of instructions or {instruction, files?, model?, name?}")
    if not body.get("max_rounds"):
        args["max_rounds"] = _DEFAULT_MAX_ROUNDS
    if not body.get("timeout_s"):
        args["timeout_s"] = _DEFAULT_TIMEOUT_S
    _attach_runners(args, body)
    return args


def _attach_runners(args: Dict[str, Any], body: Dict[str, Any]) -> None:
    """Carry `runner` from the request onto the parsed tasks.

    The delegation parser keeps only the four fields a built-in worker needs
    (`name`, `instruction`, `model`, `files`), so the runner is re-attached
    here: the job-wide `runner` applies to every task, and a per-task one
    overrides it when the request's task list lines up with the parsed one
    (the parser drops tasks with no instruction, so it may not).

    A task with no runner keeps no `runner` key at all — that is what makes a
    job without one identical to a job from before this existed.
    """
    job_wide = str(body.get("runner") or "").strip()
    tasks = args.get("tasks") or []
    raw = body.get("tasks")
    per_task: List[str] = []
    if isinstance(raw, list) and len(raw) == len(tasks):
        per_task = [str((t or {}).get("runner") or "").strip() if isinstance(t, dict) else "" for t in raw]
    for i, task in enumerate(tasks):
        chosen = (per_task[i] if i < len(per_task) and per_task[i] else job_wide)
        if chosen:
            task["runner"] = chosen


def _task_text(task: Any) -> str:
    """The words of one task the objective ids are read out of."""
    t = task if isinstance(task, dict) else {}
    return f"{t.get('name') or ''} {t.get('instruction') or ''}"


def _task_label(task: Any, index: int) -> str:
    """How one task is named in the `task_order` record — the same name the
    board, `progress` and the worker reports use, so the record can be read
    against them."""
    t = task if isinstance(task, dict) else {}
    return _squash(t.get("name") or t.get("instruction") or f"worker-{index + 1}", 60)


def order_tasks_by_impact(tasks: Any, workspace: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Order the tasks of ONE job by the impact score of the objective each
    one names, highest first.

    The score is the one `services/objectives.py` already computes over the
    project's declared dependency graph (PageRank .30 + betweenness .30 +
    blocker_ratio .20 + staleness .10 + priority .10); a task that names
    several objectives takes the highest of them, because a task is worth what
    the most important thing it unblocks is worth.

    **The scope is one job's task list and nothing else.** There is no queue
    across jobs, no global scheduler and no re-ordering of anything already
    running: ordering the tasks a caller sent together is the honest amount of
    ordering this evidence supports. A task that names no objective — or names
    one the workspace's objectives file does not have — keeps its own position;
    only the tasks that DO carry a score are permuted, among the slots they
    already occupied.

    Returns ``(tasks, record)``, where `record` is the
    ``{"by": "impact", "from": [...], "to": [...]}`` audit entry and is None
    whenever nothing was reordered. Never raises: the caller's list comes back
    unchanged if anything at all goes wrong.
    """
    rows = list(tasks or [])
    try:
        if len(rows) < 2 or not all(isinstance(t, dict) for t in rows):
            return rows, None
        from services import objectives as objectives_svc
        project = {"workspace": str(workspace or "")}
        path = objectives_svc.objectives_path(project)
        if not path or not os.path.isfile(path):
            return rows, None
        state = objectives_svc.load_state(project)
        scores = objectives_svc.impact_scores(state)
        if not scores:
            return rows, None
        scored: List[Tuple[int, float]] = []
        for i, task in enumerate(rows):
            best: Optional[float] = None
            for oid in objectives_svc.mentioned_ids(_task_text(task)):
                row = scores.get(oid)
                if not isinstance(row, dict):
                    continue          # an id this workspace does not have: no score
                try:
                    value = float(row.get("score") or 0.0)
                except (TypeError, ValueError):
                    continue
                if best is None or value > best:
                    best = value
            if best is not None:
                scored.append((i, best))
        if len(scored) < 2:
            return rows, None
        slots = [i for i, _ in scored]
        # Highest score first; ties keep the order they were written in, so
        # the same job always produces the same list.
        ranked = [i for i, _ in sorted(scored, key=lambda r: (-r[1], r[0]))]
        if ranked == slots:
            return rows, None
        out = list(rows)
        for slot, src in zip(slots, ranked):
            out[slot] = rows[src]
        record = {"by": "impact",
                  "from": [_task_label(t, i) for i, t in enumerate(rows)],
                  "to": [_task_label(t, i) for i, t in enumerate(out)]}
        return out, record
    except Exception as e:  # noqa: BLE001 - a job never fails over its own ordering
        logger.debug("dispatch: objective ordering unavailable: %s", e)
        return rows, None


def _verify_options(body: Dict[str, Any]) -> Tuple[str, str, int, float]:
    raw = body.get("verify")
    if raw is None or raw is True:
        verify = "auto"
    elif raw is False:
        verify = "none"
    else:
        verify = str(raw).strip() or "auto"
        if verify.lower() in ("auto", "none", "off", "false"):
            verify = "none" if verify.lower() in ("none", "off", "false") else "auto"
        elif len(verify) > _VERIFY_CMD_CHARS:
            raise ValueError(f"verify: the command is longer than {_VERIFY_CMD_CHARS} characters")
    scope = str(body.get("verify_scope") or "related").strip().lower()
    if scope not in ("related", "all"):
        raise ValueError("verify_scope must be 'related' or 'all'")
    try:
        fix = int(body.get("fix_rounds", _DEFAULT_FIX_ROUNDS) if body.get("fix_rounds") is not None else _DEFAULT_FIX_ROUNDS)
    except (TypeError, ValueError):
        raise ValueError("fix_rounds must be an integer")
    fix = max(0, min(_max_fix_rounds(), fix))
    try:
        vt = float(body.get("verify_timeout_s") or _VERIFY_TIMEOUT_S)
    except (TypeError, ValueError):
        raise ValueError("verify_timeout_s must be a number")
    return verify, scope, fix, max(10.0, min(vt, 3600.0))


_ALLOWED_GEN = frozenset({"temperature", "top_p", "top_k", "repeat_penalty", "seed", "num_ctx", "max_tokens", "num_predict"})


def _clean_gen(raw: Any) -> Optional[Dict[str, Any]]:
    """Sampling knobs only: `main_gpu` / `num_gpu` / `keep_alive` would let a
    request override the GPU placement policy the admin chose."""
    if not isinstance(raw, dict):
        return None
    out = {k: v for k, v in raw.items() if k in _ALLOWED_GEN and isinstance(v, (int, float, str))}
    return out or None


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


# one job at a time per workspace (nested folders count as the same one)
_running_ws: Dict[str, str] = {}          # workspace key → job id
_ws_waiters: List[asyncio.Event] = []


def _ws_key(path: Optional[str]) -> str:
    if not path:
        return ""
    k = os.path.realpath(path).replace("\\", "/").rstrip("/") + "/"
    return k.lower() if os.name == "nt" else k


def _ws_busy(key: str) -> Optional[str]:
    for other, jid in _running_ws.items():
        if other.startswith(key) or key.startswith(other):
            return jid
    return None


async def _acquire_workspace(job: DispatchJob) -> str:
    key = _ws_key(job.workspace)
    while True:
        holder = _ws_busy(key)
        if holder is None:
            _running_ws[key] = job.id
            return key
        if not job.events or job.events[-1].get("message") != f"waiting for job {holder} in the same workspace":
            job._event(event="job", message=f"waiting for job {holder} in the same workspace")
        ev = asyncio.Event()
        _ws_waiters.append(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        finally:
            if ev in _ws_waiters:
                _ws_waiters.remove(ev)


def _release_workspace(key: str, job_id: str) -> None:
    if _running_ws.get(key) == job_id:
        _running_ws.pop(key, None)
    for ev in list(_ws_waiters):
        ev.set()


# ── external agents: a worker Faustus did not write ─────────────────────────

def task_runner(task: Any) -> str:
    """The runner key of one task, or "" for the built-in sub-agent."""
    return str((task or {}).get("runner") or "").strip() if isinstance(task, dict) else ""


def runner_keys(args: Dict[str, Any]) -> List[str]:
    """Every distinct runner named by a job's tasks, in the order they appear."""
    out: List[str] = []
    for t in (args or {}).get("tasks") or []:
        key = task_runner(t)
        if key and key not in out:
            out.append(key)
    return out


def vet_runners(args: Dict[str, Any]) -> None:
    """Refuse a job whose runners cannot do the work, BEFORE anything starts.

    A missing runner must not cost the job's other workers their time and
    tokens for a result that was never going to be complete, so this raises
    ValueError (a 400 with the reason) instead of failing one task halfway
    through. It says exactly what to do: turn the setting on, or install the
    agent with its `ollama launch` line.
    """
    keys = runner_keys(args)
    if not keys:
        return
    if not external_runners_on():
        raise ValueError(
            "this job asks for an external agent runner (" + ", ".join(keys) + ") and "
            "`agent_external_runners` is off. It ships off because it runs third-party binaries on "
            "this machine, and Faustus's command guard cannot see inside another agent's own shell. "
            "Turn it on in Settings → Agent & automation."
        )
    from src import agent_runners as reg
    for key in keys:
        runner = reg.get(key)
        if runner is None:
            known = ", ".join(r.key for r in reg.runners()[:24])
            raise ValueError(f"unknown agent runner: {key!r}. Known: {known}")
        row = reg.to_row(runner)
        if not row["invocation_known"]:
            raise ValueError(f"{runner.label} is {reg.NOT_RUNNABLE_NOTE}: Faustus has no row saying how "
                             f"to run one task with it (src/agent_runners.py)")
        if not row["installed"]:
            raise ValueError(f"{runner.label} is not installed on this machine. Install it with: "
                             f"{row['install']}")


def _external_resume_supported() -> bool:
    """Whether THIS build's `external_worker.run_task` can continue a run.

    Asked of the signature rather than assumed, because the two halves ship
    separately: the dispatch side carries the handle and prefers resume the
    moment the runner table and the worker can act on it, and until then a job
    behaves exactly as it does today instead of raising TypeError at the worst
    possible moment.
    """
    try:
        import inspect
        from src import external_worker
        return "resume" in inspect.signature(external_worker.run_task).parameters
    except Exception as exc:  # noqa: BLE001 - a capability probe never fails a job
        logger.debug("dispatch: external resume probe failed: %s", exc)
        return False


def _external_timeout(job: "DispatchJob") -> float:
    """The hard bound on ONE external agent: the smaller of the job's
    per-worker timeout and `agent_external_runner_timeout_s`. Never raises —
    a settings read that fails leaves the job's own timeout standing."""
    try:
        per_worker = float(job.args.get("timeout_s") or _DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        per_worker = float(_DEFAULT_TIMEOUT_S)
    try:
        from src import agent_runners as reg
        return max(1.0, min(per_worker, float(reg.timeout_s())))
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch %s: external timeout unavailable: %s", job.id, e)
        return max(1.0, per_worker)


def _external_report(task: Dict[str, Any], index: int, result: Dict[str, Any]) -> Dict[str, Any]:
    """One external run, in the shape the compact answer already reads.

    `mutations` is EMPTY on purpose: an external agent files no claim about
    what it changed, so the observed diff is the whole story and
    `claimed_only` has nothing to accuse it of. Rounds, tool calls and tokens
    are 0 for the same reason — Faustus did not run that loop and does not get
    to report numbers for it.
    """
    status = str(result.get("status") or ("done" if result.get("ok") else "error"))
    summary = _squash(result.get("output_tail"), EXTERNAL_SUMMARY_CHARS)
    return {
        "id": index, "name": str(task.get("name") or f"worker-{index + 1}"),
        "session_id": None, "status": status,
        "stop_reason": ("complete" if status == "done" else status),
        "error": _squash(result.get("error"), 300) or None,
        "tool_calls": 0, "failed_calls": 0, "rounds": 0, "mutations": [], "rejections": [],
        "input_tokens": 0, "output_tokens": 0, "duration_s": result.get("seconds"),
        "model": str(task.get("model") or ""), "role": "external",
        "final_text": summary, "instruction": str(task.get("instruction") or ""),
        "files": list(task.get("files") or []), "supervisor": [],
        "outcome": result.get("outcome"),
        # What the guard could not see, on the worker row itself.
        "runner": result.get("runner") or task_runner(task),
        "runner_label": result.get("label") or "",
        "argv_shown": result.get("argv_shown") or "",
        "unguarded": True,
        "exit_code": result.get("exit_code"),
        "timed_out": bool(result.get("timed_out")),
        "state": result.get("state") or "",
        "why": result.get("why") or "",
        # The runner's own session identity, when it reports one. Absent from
        # every runner today (see the report accompanying this change: one line
        # in src/agent_runners.py and one in src/external_worker.py), and the
        # empty string is what makes `_resume_target` fall through to today's
        # fresh fixer rather than to a handle nobody can use.
        "runner_session": str(result.get("session") or result.get("session_id") or ""),
    }


async def _run_external(job: DispatchJob, tasks: List[Dict[str, Any]], cb: Callable) -> Dict[str, Any]:
    """Run the tasks that name an external agent, one at a time.

    Sequential even when the job is parallel: these are whole agent processes
    with their own models and their own shells in the SAME workspace, and
    nothing here can hold a file lock over what one of them does. One at a
    time is the only honest concurrency this path can offer.
    """
    from src import agent_runners as reg
    from src import external_worker
    reports: List[Dict[str, Any]] = []
    for i, task in enumerate(tasks):
        key = task_runner(task)
        name = str(task.get("name") or f"worker-{i + 1}")
        # Recorded BEFORE the agent starts, never after it finishes: a job
        # cancelled mid-run still has to say that something ran unguarded.
        if key and key not in job.runners_used:
            job.runners_used.append(key)
        await cb({"subagent": {"event": "started", "name": name, "runner": key,
                               "message": f"external agent `{key}` — {reg.GUARD_NOTE}"}})

        def _emit(line: str, _name: str = name) -> None:
            # The agent's own words, on the board: the state rules read them
            # (rate limited / waiting for input / stuck) and REPORT, never kill.
            try:
                job.note_worker_event({"event": "tool", "name": _name, "tail": str(line)[-2000:]})
            except Exception as e:  # noqa: BLE001 - a board event never breaks a run
                logger.debug("dispatch %s: external output event failed: %s", job.id, e)

        extra: Dict[str, Any] = {}
        handle = task.get("resume") if isinstance(task.get("resume"), dict) else None
        if handle and handle.get("kind") == "runner" and handle.get("id") and _external_resume_supported():
            # `claude -p --resume <id>`, OpenCode's task id: continuing the
            # agent that made the change instead of starting a new one that has
            # to read its way back to the same understanding.
            extra["resume"] = str(handle["id"])
        result = await asyncio.to_thread(
            external_worker.run_task, key, str(task.get("instruction") or ""),
            workspace=job.workspace, model=str(task.get("model") or "") or None,
            **extra,
            # Two ceilings apply and the smaller wins: the job's own
            # per-worker timeout, and `agent_external_runner_timeout_s` (the
            # bound the operator put on any third-party binary). Neither one
            # may be raised by the other.
            timeout_s=_external_timeout(job),
            on_output=_emit,
            should_cancel=lambda: job.status in ("cancelling", "cancelled"),
        )
        report = _external_report(task, i, result)
        reports.append(report)
        await cb({"subagent": {"event": "done", "name": name, "status": report["status"],
                               "runner": key,
                               "message": _squash(report.get("error") or report["final_text"], 200)}})
    ok = all(r["status"] == "done" for r in reports)
    return {"subagents": reports, "exit_code": 0 if ok else 1,
            "output": f"{len(reports)} external agent worker(s) ran outside the command guard",
            "dropped_tasks": 0}


async def _work(job: DispatchJob, args: Dict[str, Any], cb: Callable) -> Dict[str, Any]:
    """One round of workers: the built-in sub-agents, the external agents, or
    both. With no task naming a runner this is exactly `_delegate(job, args,
    cb)` and nothing else runs."""
    tasks = list(args.get("tasks") or [])
    external = [t for t in tasks if task_runner(t)]
    if not external:
        return await _delegate(job, args, cb)
    internal = [t for t in tasks if not task_runner(t)]
    result: Dict[str, Any] = {"subagents": [], "exit_code": 0, "output": ""}
    if internal:
        result = await _delegate(job, dict(args, tasks=internal), cb)
        if not isinstance(result, dict):
            result = {"subagents": [], "exit_code": 1, "output": str(result)}
    ext = await _run_external(job, external, cb)
    merged = dict(result)
    merged["subagents"] = list(result.get("subagents") or []) + list(ext.get("subagents") or [])
    merged["output"] = " · ".join(x for x in (str(result.get("output") or ""), str(ext.get("output") or "")) if x)
    merged["exit_code"] = 0 if (result.get("exit_code") in (0, None) and ext.get("exit_code") == 0) else 1
    return merged


async def _delegate(job: DispatchJob, args: Dict[str, Any], cb: Callable) -> Dict[str, Any]:
    from src.agent_tools.subagent_tools import DelegateAgentsTool
    tool = DelegateAgentsTool()
    ctx = {"session_id": job.session_id, "owner": job.owner, "progress_cb": cb,
           "gen_overrides": job.gen_overrides or None, "model": job.model}
    result = await tool.execute(json.dumps(args), ctx)
    return result if isinstance(result, dict) else {"output": str(result)}


def _worker_statuses(result: Optional[Dict[str, Any]]) -> List[str]:
    return [str(r.get("status") or "") for r in (result or {}).get("subagents") or [] if isinstance(r, dict)]


def _build_proof(job: "DispatchJob") -> Optional[Dict[str, Any]]:
    """The `prove` step (src/prove.py): reconcile what Faustus OBSERVED on disk
    and what the verification did with what the workers CLAIMED, and say what
    that proves — with every reason the confidence is not 1 named.

    The claims are the workers' own `mutations` lists: their word, which is
    exactly the thing being checked, never the source of the answer.
    """
    if not prove_on():
        return None
    try:
        from src import prove
        claimed: List[str] = []
        workers: List[Dict[str, Any]] = []
        for r in (job.result or {}).get("subagents") or []:
            if not isinstance(r, dict):
                continue
            workers.append({"name": r.get("name"), "status": r.get("status"), "outcome": _worker_outcome(r)})
            for p in list(r.get("mutations") or [])[:40]:
                p = str(p)
                if p not in claimed:
                    claimed.append(p)
        if job.status in ("cancelled", "cancelling", "interrupted"):
            # The job itself was stopped: that is not a worker's failure, and
            # it is not something the proof may leave out either.
            workers.append({"name": "job", "status": job.status, "outcome": "cancelled"})
        packet = prove.prove(job.changes, job.verification, {"paths": claimed, "workers": workers})
        return _note_unguarded(packet, job.runners_used)
    except Exception as e:  # noqa: BLE001 - the settle path never fails over the proof
        logger.debug("dispatch %s: proof unavailable: %s", job.id, e)
        return None


def _note_unguarded(packet: Optional[Dict[str, Any]], runners: List[str]) -> Optional[Dict[str, Any]]:
    """Add the `external_agent_unguarded` entry to a proof, and pay for it.

    src/prove.py names every reason its confidence is not 1. The reason it
    cannot name by itself is the one this module knows: an agent Faustus did
    not write ran its own shell, and the command guard saw none of it. Hiding
    that would make the proof a lie about exactly the thing this app is
    careful about — so the entry goes in, the confidence drops by the same
    weight prove gives an unnamed uncertainty, and the list is re-sorted the
    way prove sorts it (heaviest first) so it reads as one list, not as two.

    Nothing here raises: a proof that cannot be annotated is returned as it is.
    """
    if not packet or not runners:
        return packet
    try:
        from src import prove
        entry = {"kind": EXTERNAL_UNGUARDED,
                 "detail": EXTERNAL_UNGUARDED_DETAIL + " (" + ", ".join(runners[:4]) + ")"}
        unc = list(packet.get("uncertainty") or [])
        if any(u.get("kind") == EXTERNAL_UNGUARDED for u in unc):
            return packet
        unc.append(entry)
        unc.sort(key=lambda u: (-prove.PENALTY.get(str(u.get("kind")), EXTERNAL_UNGUARDED_PENALTY),
                                str(u.get("kind"))))
        try:
            confidence = float(packet.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        packet["uncertainty"] = unc
        packet["confidence"] = round(max(0.0, confidence - EXTERNAL_UNGUARDED_PENALTY), 3)
        packet["unguarded_runners"] = list(runners)
    except Exception as e:  # noqa: BLE001 - the settle path never fails over the proof
        logger.debug("dispatch: could not note the unguarded runners: %s", e)
    return packet


def _settle(job: DispatchJob) -> None:
    """The honest top-level answer, from the worker set and the verification —
    never from `exit_code` alone (a stalled or stopped worker has no error)."""
    statuses = _worker_statuses(job.result)
    if job.status in ("cancelled", "interrupted"):
        pass
    elif not statuses:
        job.status = "error"
        job.error = job.error or _squash((job.result or {}).get("error"), 500) or "no worker ran"
    else:
        bad = [s for s in statuses if s != "done"]
        v = job.verification
        if bad or verification_failed(v):
            job.status = "partial"
        else:
            job.status = "done"
    job.proof = _build_proof(job)
    parts = []
    n = len(statuses)
    if n:
        parts.append(f"{n - len([s for s in statuses if s != 'done'])}/{n} workers done"
                     + (" (" + ", ".join(sorted({s for s in statuses if s != 'done'})) + ")" if any(s != "done" for s in statuses) else ""))
    if job.changes is not None:
        n_ch = int(job.changes.get("count") or 0)
        parts.append(f"{n_ch} file{'s' if n_ch != 1 else ''} changed on disk")
    v = job.verification
    if v:
        if v.get("ran") and v.get("ok") is True:
            parts.append(f"verification passed ({v.get('summary') or v.get('command')})")
        elif verification_failed(v):
            parts.append(f"verification FAILED ({v.get('summary')})")
        elif v.get("ran"):
            parts.append(f"verification inconclusive ({v.get('summary')})")
        else:
            parts.append(f"not verified: {v.get('summary')}")
    c = job.convergence
    if c:
        parts.append(f"fix rounds converged ({c.get('score')}, {c.get('confidence')})"
                     if job.stopped_by == "convergence"
                     else f"fix-round convergence {c.get('score')} ({c.get('confidence')})")
    if job.proof:
        # The verdict says what is PROVED, not only what happened: a job whose
        # workers finished and whose tests could not run is `done` and
        # `unproved`, and both words belong on the line.
        try:
            from src import prove
            proof_line = prove.line(job.proof, detail_chars=70)
            if proof_line:
                parts.append(proof_line)
        except Exception as e:  # noqa: BLE001
            logger.debug("dispatch %s: proof line unavailable: %s", job.id, e)
    if job.task_order:
        # Only when it actually changed something (task_order is None
        # otherwise): the human reading the line must be able to see that the
        # tasks did not run in the order they were written, and why.
        parts.append("tasks ordered by objective impact: "
                     + _squash(" → ".join(str(n) for n in job.task_order.get("to") or []), 160))
    if job.runners_used:
        # Said on the line a human reads, not only inside the proof packet: the
        # verification and the diff below are Faustus's; the commands that
        # produced them were not seen by anything.
        parts.append("external agent(s) ran unguarded: " + ", ".join(job.runners_used[:4]))
    blocked = _blocked_workers(job)
    if blocked:
        # Reported, not acted on: a rate-limited or prompting worker was never
        # killed for it, and the verdict says so instead of hiding it.
        parts.append("reported (not killed): " + ", ".join(f"{n} {s}" for n, s in blocked[:4]))
    if job.status == "cancelled":
        parts.insert(0, "cancelled")
    elif job.status == "error":
        parts.insert(0, f"error: {job.error}")
    job.verdict = " · ".join(parts)[:400] or job.status
    # Objectives evidence (services/objectives.py): a task that names an
    # OBJ-id gets its outcome recorded on that objective's audit log. Pure
    # bookkeeping — a failure here must never affect the dispatch result.
    try:
        _record_objective_evidence(job)
    except Exception as e:  # noqa: BLE001 - settle path, never raise
        logger.debug("objective evidence for %s failed: %s", job.id, e)


def _blocked_workers(job: DispatchJob) -> List[Tuple[str, str]]:
    """(worker, state) for every worker whose own output said it could not
    progress by itself at some point — `rate_limited`, `waiting_for_input`,
    `stuck`. A report for the human and the coordinator; never a verdict on
    the worker, and never a reason to stop one."""
    try:
        from src import output_rules
        out: List[Tuple[str, str]] = []
        for name, st in sorted((job.worker_states or {}).items()):
            for state in st.get("seen") or ():
                if state in output_rules.BLOCKED_STATES:
                    out.append((name, state))
        return out
    except Exception as e:  # noqa: BLE001 - the verdict never fails over a note
        logger.debug("dispatch %s: blocked-worker note failed: %s", job.id, e)
        return []


def worker_states(job: DispatchJob) -> Dict[str, Dict[str, Any]]:
    """Each worker's detected state, its `why` and the literal that proves it
    — the block `?states=1` adds to the events answer. Empty with
    `agent_worker_state_detection` off."""
    out: Dict[str, Dict[str, Any]] = {}
    for name, st in (job.worker_states or {}).items():
        if not st.get("state"):
            continue
        out[name] = {"state": st.get("state"), "why": st.get("why") or "",
                     "matched": st.get("matched") or "", "confidence": st.get("confidence"),
                     "seen": list(st.get("seen") or ())}
    return out


def _record_objective_evidence(job: DispatchJob) -> None:
    """Append evidence records for every OBJ-<n> the job's tasks name, when
    the job's chat belongs to a project that has that objective."""
    from services import objectives as objectives_svc
    texts = " ".join(_task_text(t) for t in job.args.get("tasks") or [] if isinstance(t, dict))
    ids = sorted(objectives_svc.mentioned_ids(texts))
    if not ids:
        return
    from services.projects import project_for_session
    project = project_for_session(job.session_id or "", job.owner)
    if not project:
        return
    state = objectives_svc.load_state(project)
    changes = job.changes or {}
    changed = list(changes.get("added") or []) + list(changes.get("modified") or [])
    note = (f"{len(changed)} file(s) changed: " + ", ".join(changed[:8])) if changed \
        else "no files changed on disk"
    confidence = 0.6 if job.status == "done" else 0.4
    for oid in ids:
        if oid in (state.get("objectives") or {}):
            objectives_svc.add_evidence(project, oid, "dispatch", job.id, confidence, note)


async def _run(job: DispatchJob) -> None:
    ws_key: Optional[str] = None
    token = roots_token = None
    evidence = _Evidence(job.workspace, job.id)
    job._entered = True
    try:
        from src import tool_execution as te
        ws_key = await _acquire_workspace(job)
        job.started = time.time()
        job.status = "running"
        job._event(event="job", message="checkpointing the workspace")
        job._persist()
        await asyncio.to_thread(evidence.before)
        job.checkpoint = evidence.sha
        job._event(event="job", message="workers running")

        async def _cb(payload: Dict[str, Any]) -> None:
            ev = payload.get("subagent") if isinstance(payload, dict) else None
            if isinstance(ev, dict):
                job.note_worker_event(dict(ev))

        token = te._active_workspace.set(job.workspace or None)
        roots_token = te._active_workspace_roots.set((job.workspace,) if job.workspace else ())
        # `_work` is `_delegate` when no task names a runner — the same call,
        # the same result, byte for byte.
        job.result = await _work(job, job.args, _cb)
        if job.result.get("error") and not job.result.get("subagents"):
            job.error = _squash(job.result.get("error"), 500)
        # evidence + verification, then the bounded fix loop
        job.status = "verifying"
        job._event(event="job", message="checking what changed on disk")
        job.changes = await asyncio.to_thread(evidence.after)
        if job.verify != "none":
            job._event(event="job", message="running the verification")
            job.verification = await _verify(job)
            attempt = 0
            convergence_on = _convergence_on()
            round_artifacts: List[str] = []
            while verification_failed(job.verification) and attempt < job.fix_rounds and job.result.get("subagents"):
                attempt += 1
                job._event(event="job", message=f"verification failed — fix round {attempt}")
                # Reach the worker that made the change, in its own session,
                # instead of building a new one from the original tasks plus
                # the failure text. Degrades to exactly that when nothing is
                # resumable — which is every job whose workers reported no
                # session handle at all.
                target = _resume_target(job, job.verification) if resume_enabled() else None
                fixer: Dict[str, Any] = {
                    "name": f"fixer-{attempt}", "files": [],
                    "model": (target or {}).get("model") or "",
                    "instruction": _fixer_instruction(job, job.verification, attempt),
                }
                if target:
                    fixer["resume"] = {"kind": target["kind"], "id": target["id"],
                                       "runner": target["runner"]}
                    if target.get("agent"):
                        fixer["agent"] = target["agent"]
                    if target["kind"] == "runner":
                        fixer["runner"] = target["runner"]
                    job._event(event="job",
                               message=f"fix round {attempt} continues `{target['name']}` in its own session")
                fixer_args = dict(job.args)
                fixer_args.update({"tasks": [fixer], "parallel": False, "reviewer": False,
                                   "dropped_tasks": 0})
                fix = await (_run_external(job, [fixer], _cb) if fixer.get("runner")
                             else _delegate(job, fixer_args, _cb))
                for r in fix.get("subagents") or []:
                    if isinstance(r, dict):
                        r["role"] = "fixer"
                        job.result.setdefault("subagents", []).append(r)
                for c in fix.get("lock_conflicts") or []:
                    job.result.setdefault("lock_conflicts", []).append(c)
                job.changes = await asyncio.to_thread(evidence.after)
                job._event(event="job", message=f"verifying again after fix round {attempt}")
                again = await _verify(job)
                again["attempts"] = attempt + 1
                again["previous"] = [{"summary": job.verification.get("summary"), "failures": job.verification.get("failures")}] \
                    + list(job.verification.get("previous") or [])
                job.verification = again
                # Convergence (src/convergence.py): when successive rounds stop
                # producing change, the rounds still on the counter would only
                # spend workers. `fix_rounds` is the maximum, not a quota.
                if convergence_on:
                    round_artifacts.append(_round_artifact(job, again))
                    verdict = _assess_convergence(round_artifacts)
                    if verdict is not None:
                        job.convergence = verdict
                        if verdict.get("converged") and attempt < job.fix_rounds:
                            job.stopped_by = "convergence"
                            job._event(event="job",
                                       message=f"fix rounds converged after {attempt} — {verdict.get('reason')}")
                            break
        else:
            job.verification = {"mode": "off", "ran": False, "ok": None,
                                "summary": "verification disabled by the request (verify: none)"}
    except asyncio.CancelledError:
        job.status = "cancelled"
        try:
            job.changes = evidence.after()
        except Exception:
            pass
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("dispatch %s failed", job.id)
        job.status = "error"
        job.error = str(e)[:500]
    finally:
        try:
            from src import tool_execution as te
            if token is not None:
                te._active_workspace.reset(token)
            if roots_token is not None:
                te._active_workspace_roots.reset(roots_token)
        except Exception:
            pass
        if ws_key is not None:
            _release_workspace(ws_key, job.id)
        job.finished = time.time()
        _settle(job)
        _record_job_duration(job)
        _record_turn(job)
        job._persist()
        job._notify()


async def _verify(job: DispatchJob) -> Dict[str, Any]:
    changed = []
    if job.changes:
        changed = list(job.changes.get("added") or []) + list(job.changes.get("modified") or [])
    return await asyncio.to_thread(run_verification, job.workspace, job.verify, changed, scope=job.verify_scope,
                                   timeout_s=job.verify_timeout_s, checkpoint_sha=job.checkpoint)


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
        comp = compact(job)["result"]
        lines = [f"Dispatched job {job.id}: {job.status}" + (f" — {job.verdict}" if job.verdict else "")]
        for w in comp.get("workers") or []:
            lines.append(f"- {w.get('name')}: {w.get('status')}"
                         + (f" — changed {', '.join(w.get('files_changed') or [])}" if w.get("files_changed") else ""))
        if job.changes is not None:
            lines.append(f"Changed on disk: {job.changes.get('count', 0)}"
                         + (" — " + ", ".join(((job.changes.get('added') or []) + (job.changes.get('modified') or []))[:20]) if job.changes.get("count") else ""))
        v = job.verification
        if v:
            lines.append(f"Verification: {v.get('summary')}" + (f" (`{v.get('command')}`)" if v.get("command") else ""))
        ev = {
            "round": 1, "model": job.model, "tool": "delegate_agents",
            "desc": f"{len(job.args.get('tasks') or [])} worker(s) dispatched from outside Faustus",
            "command": json.dumps({"tasks": [t.get("instruction", "")[:300] for t in job.args.get("tasks") or []],
                                   "parallel": bool(job.args.get("parallel"))}, ensure_ascii=False),
            "output": str(result.get("output") or job.error or job.status)[:4000],
            "exit_code": 0 if job.status == "done" else 1,
            "subagents": _compact_subagent_reports(reports) if reports else [],
            "dispatch_id": job.id,
        }
        # with tool_events present the renderer takes the bubbles from
        # round_texts (never from content): the verdict is the text of a
        # second, tool-less round, i.e. it appears under the board
        meta = {"tool_events": [ev], "model": job.model, "source": "dispatch", "dispatch_id": job.id,
                "round_texts": ["", "\n".join(lines)], "round_models": [job.model, job.model]}
        # The same `harness` block a chat turn persists (chatRenderer reads
        # metadata.harness): the 🛡 badge, the edited-file chips with "diff vs
        # before this turn" against the job's checkpoint, the tests line.
        hz: Dict[str, Any] = {
            "stop_reason": "complete" if job.status == "done" else job.status,
            "mutations": list(comp.get("files_changed") or []),
            "tool_calls": int((comp.get("totals") or {}).get("tool_calls") or 0),
            "failed_calls": int((comp.get("totals") or {}).get("failed_calls") or 0),
            "rejections": 0, "workspace": job.workspace, "checkpoint": job.checkpoint,
            "notes": [job.verdict] if job.verdict else [],
        }
        if job.changes and job.changes.get("git"):
            hz["git"] = {"changed_count": job.changes["git"].get("dirty_count"), "shortstat": job.changes["git"].get("shortstat"), "changed": []}
        if v and v.get("ran"):
            hz["tests"] = {k: v.get(k) for k in ("ran", "ok", "summary", "command", "failures", "kind", "scope", "output_tail",
                                                 "inconclusive", "related_files", "duration_s", "pre_existing", "pre_existing_only",
                                                 "new_failures", "timed_out") if k in v}
            hz["tests"]["label"] = v.get("command") or v.get("kind")
            hz["tests_fix_rounds"] = max(0, int(v.get("attempts") or 1) - 1)
        meta["harness"] = hz
        # the footer needs the usual metrics to render at all (and the 🛡
        # badge hangs off that footer): the job's duration and local tokens
        tot = comp.get("totals") or {}
        meta.update({"response_time": round((job.finished or time.time()) - (job.started or job.created), 2),
                     "input_tokens": int(tot.get("input_tokens") or 0), "output_tokens": int(tot.get("output_tokens") or 0),
                     "usage_source": "real"})
        sm.add_message(job.session_id, ChatMessage("assistant", "\n".join(lines), metadata=meta))
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
        # The coordinator's words are NOT the human's: marked the way the
        # repo marks any text that did not come from the user (the tool
        # gate reads `trusted: False` + provenance), so a human who opens
        # the board and continues the chat does not lend them their own
        # standing.
        sm.add_message(sid, ChatMessage(role="user", content=_dispatch_note(job),
                                        metadata={"source": "dispatch", "dispatch_id": job.id, "trusted": False,
                                                  "provenance_origin": "external", "tool_gate_untrusted": True,
                                                  "display": "system_card"}))
    except Exception:
        pass
    return sid


def _dispatch_note(job: DispatchJob) -> str:
    lines = [f"Dispatched from outside Faustus (job {job.id}) — {len(job.args.get('tasks') or [])} task(s), "
             f"workspace: {job.workspace or '—'}, model: {job.model}. "
             "(External instructions: not written by the user of this chat.)"]
    for i, t in enumerate(job.args.get("tasks") or [], 1):
        lines.append(f"{i}. {_squash(t.get('instruction'), 300)}")
    if job.verify != "none":
        lines.append(f"Verification: {'auto-detected test runner' if job.verify == 'auto' else job.verify}"
                     f" ({job.verify_scope}), fix rounds: {job.fix_rounds}")
    return "\n".join(lines)


def _idempotent_get(owner: Optional[str], key: str) -> Optional["DispatchJob"]:
    now = time.time()
    for k, (jid, ts) in list(_idempotent.items()):
        if now - ts > _IDEMPOTENCY_TTL_S:
            _idempotent.pop(k, None)
    hit = _idempotent.get((owner or "", key))
    return get(hit[0]) if hit else None


async def start(owner: Optional[str], body: Dict[str, Any], *, runner: Optional[Callable] = None,
                idempotency_key: Optional[str] = None) -> DispatchJob:
    """Validate, create the Workers chat and launch the job in the background.
    With an idempotency key already seen for this owner, the job it started
    is returned instead of a second one."""
    key = str(idempotency_key or body.get("client_request_id") or "").strip()[:200]
    if key:
        existing = _idempotent_get(owner, key)
        if existing is not None:
            return existing
    args = build_args(body)
    # An external agent that is off, unknown or not installed is refused here,
    # before a Workers chat exists and before any other worker starts.
    vet_runners(args)
    raw_ws = str(body.get("workspace") or "").strip()
    if not raw_ws:
        # without one the workers' cwd is Faustus's own data dir (sessions,
        # settings, auth) — never a place for a worker
        raise ValueError("workspace is required: the absolute folder the workers are confined to")
    from src.tool_execution import vet_workspace
    workspace = vet_workspace(raw_ws)
    if not workspace:
        raise ValueError(f"workspace is not a usable directory: {raw_ws}")
    verify, scope, fix_rounds, verify_timeout = _verify_options(body)
    url, model, headers = resolve_route(owner, body.get("model"))
    gen = _clean_gen(body.get("gen_overrides"))
    # The impact score the objectives graph already computes finally decides
    # something: a SEQUENTIAL job's tasks run in the order the graph ranks the
    # objectives they name (`agent_objective_ordering`). A parallel job has no
    # order to fix, so it is left exactly as it was sent.
    task_order = None
    if objective_ordering_on() and not args.get("parallel"):
        ordered, task_order = order_tasks_by_impact(args.get("tasks"), workspace)
        if task_order is not None:
            args = dict(args, tasks=ordered)
    job = DispatchJob(owner, args, workspace, url, model, headers, _title(args), gen,
                      verify=verify, verify_scope=scope, fix_rounds=fix_rounds, verify_timeout_s=verify_timeout)
    job.task_order = task_order
    job.session_id = _make_session(job)
    async with _lock:
        _jobs[job.id] = job
        if key:
            _idempotent[(owner or "", key)] = (job.id, time.time())
        job._persist()
        _evict()
    job.task = asyncio.create_task((runner or _run)(job))
    return job


def _evict() -> None:
    """Keep MAX_JOBS_KEPT finished jobs — in memory AND on disk (the mirrors
    used to come straight back through list_jobs → _load_all)."""
    if len(_jobs) > MAX_JOBS_KEPT:
        for old in sorted(_jobs.values(), key=lambda j: j.created)[: len(_jobs) - MAX_JOBS_KEPT]:
            if old.status not in _LIVE:
                _jobs.pop(old.id, None)
    try:
        d = _data_dir()
        names = [n for n in os.listdir(d) if n.endswith(".json")]
        if len(names) <= MAX_JOBS_KEPT:
            return
        paths = sorted((os.path.getmtime(os.path.join(d, n)), n) for n in names)
        for _, n in paths[: len(paths) - MAX_JOBS_KEPT]:
            jid = n[:-5]
            live = _jobs.get(jid)
            if live is not None and live.status in _LIVE:
                continue
            try:
                os.remove(os.path.join(d, n))
            except OSError:
                pass
    except OSError:
        pass


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
                      d.get("workspace"), "", d.get("model") or "", None, d.get("title") or "Workers",
                      verify=str(d.get("verify") or "auto"), verify_scope=str(d.get("verify_scope") or "related"),
                      fix_rounds=int(d.get("fix_rounds") or 0))
    job.id = d.get("id") or job_id
    job.created = float(d.get("created") or 0)
    job.started = d.get("started")
    job.finished = d.get("finished")
    job.session_id = d.get("session_id")
    job.error = d.get("error")
    job.verdict = d.get("verdict")
    job.result = d.get("result")
    job.changes = d.get("changes")
    job.verification = d.get("verification")
    job.checkpoint = d.get("checkpoint")
    job.convergence = d.get("convergence")
    job.stopped_by = d.get("stopped_by")
    job.proof = d.get("proof")
    job.task_order = d.get("task_order")
    # What ran outside the command guard has to survive a restart: a job read
    # back from its mirror still says which external agents it used.
    job.runners_used = [str(k) for k in (d.get("runners") or []) if str(k)]
    # a job that was running when the server stopped never finished
    job.status = "interrupted" if d.get("status") in _LIVE else (d.get("status") or "done")
    if job.status == "interrupted" and not job.verdict:
        job.verdict = "interrupted by a restart of Faustus — re-dispatch the remaining work"
    _jobs[job.id] = job
    return job


def recent_counts(window_s: float = 3600.0, *, now: Optional[float] = None) -> Dict[str, int]:
    """How the jobs of the last `window_s` ended, from what is ALREADY in
    memory — no listdir, no mirror is read. A caller polling this every few
    seconds (the usage widget) must not turn a health reading into disk work,
    and a process that has run no job answers `jobs: 0`, which is honestly "no
    signal" rather than "everything is fine"."""
    t = time.time() if now is None else now
    out = {"jobs": 0, "done": 0, "partial": 0, "failed": 0, "cancelled": 0, "live": 0}
    for job in list(_jobs.values()):
        stamp = job.finished or job.started or job.created or 0.0
        if not stamp or t - float(stamp) > float(window_s):
            continue
        out["jobs"] += 1
        if job.status in _LIVE:
            out["live"] += 1
        elif job.status == "done":
            out["done"] += 1
        elif job.status == "partial":
            out["partial"] += 1
        elif job.status in ("cancelled", "cancelling"):
            out["cancelled"] += 1
        else:                                   # error, interrupted
            out["failed"] += 1
    return out


def visible_to(job: DispatchJob, owner: Optional[str]) -> bool:
    """One predicate for the list and the by-id read: a named owner sees
    only their jobs; single-user / anonymous mode (owner "" or None) sees
    everything. A job with no owner is nobody's in multi-user mode."""
    if not owner:
        return True
    return job.owner == owner


def list_jobs(owner: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    _load_all()
    rows = [j for j in _jobs.values() if visible_to(j, owner)]
    rows.sort(key=lambda j: j.created, reverse=True)
    return [j.to_dict(include_result=False, brief=True) for j in rows[:limit]]


def _load_all() -> None:
    """The newest MAX_JOBS_KEPT mirrors, at most once every 2 s (the Workers
    page polls every 3 s; a listdir per poll is fine, a stat per file is not)."""
    global _loaded_all_at
    if time.time() - _loaded_all_at < 2.0:
        return
    _loaded_all_at = time.time()
    try:
        d = _data_dir()
        names = [n for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return
    missing = [n for n in names if n[:-5] not in _jobs]
    if len(missing) > MAX_JOBS_KEPT:
        try:
            missing = [n for _, n in sorted(((os.path.getmtime(os.path.join(d, n)), n) for n in missing), reverse=True)[:MAX_JOBS_KEPT]]
        except OSError:
            missing = missing[:MAX_JOBS_KEPT]
    for name in missing:
        _load(name[:-5])
    if len(_jobs) > MAX_JOBS_KEPT:
        for old in sorted(_jobs.values(), key=lambda j: j.created)[: len(_jobs) - MAX_JOBS_KEPT]:
            if old.status not in _LIVE:
                _jobs.pop(old.id, None)


async def wait(job: DispatchJob, timeout: float) -> bool:
    """True when the job is finished (possibly already). A cancelled job
    counts as finished only once its workers have unwound."""
    if job.status not in _LIVE:
        return True
    ev = asyncio.Event()
    job._waiters.append(ev)
    try:
        await asyncio.wait_for(ev.wait(), timeout=max(0.0, timeout))
        return True
    except asyncio.TimeoutError:
        return job.status not in _LIVE
    finally:
        if ev in job._waiters:
            job._waiters.remove(ev)


# ── wait_for: block on a condition, not on a sleep ──────────────────────────

def parse_condition(raw: Any) -> Dict[str, Any]:
    """Parse a `wait_for` condition, or raise ValueError naming every form.

    `done` (the default), `changed`, `phase:<name>`, `event:<text>` and
    `worker:<label>:<state>` — where `<state>` is one src/output_rules.py can
    actually report and `<label>` is a worker's name (`*` for any of them).
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        text = "done"
    low = text.lower()
    if low == "done":
        return {"kind": "done", "raw": "done"}
    if low == "changed":
        return {"kind": "changed", "raw": "changed"}
    head, sep, rest = text.partition(":")
    head = head.strip().lower()
    rest = rest.strip()
    if sep and head in ("phase", "event") and rest:
        return {"kind": head, "text": rest.lower(), "raw": text}
    if sep and head == "worker" and rest:
        label, sep2, state = rest.rpartition(":")
        label, state = label.strip(), state.strip().lower()
        if sep2 and label:
            from src import output_rules
            if state in output_rules.STATES:
                return {"kind": "worker", "label": label, "state": state, "raw": text}
    raise ValueError(_condition_error(text))


def _condition_error(text: str) -> str:
    from src import output_rules
    return (f"unknown wait condition {text[:80]!r} — use one of: {', '.join(WAIT_CONDITIONS)}; "
            f"<state> is one of {', '.join(output_rules.STATES)}")


def _event_text(ev: Dict[str, Any]) -> str:
    """The words of one event — its values, not its keys, so `event:tool` asks
    about a tool the worker ran and not about the shape of the record."""
    bits: List[str] = []
    for value in ev.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (str, int, float)):
            bits.append(str(value))
        elif isinstance(value, (list, tuple)):
            bits.extend(str(x) for x in value if isinstance(x, (str, int, float)) and not isinstance(x, bool))
    return " ".join(bits)


def condition_holds(job: DispatchJob, cond: Dict[str, Any], changed: bool = False) -> bool:
    """Does the condition hold right now? Pure, cheap and never raising: it
    runs on every progress update of the job."""
    try:
        kind = cond.get("kind")
        if kind == "done":
            return job.status not in _LIVE
        if kind == "changed":
            return bool(changed)
        if kind == "phase":
            needle = str(cond.get("text") or "")
            return any(ev.get("name") == "job" and needle in str(ev.get("message") or "").lower()
                       for ev in job.events)
        if kind == "event":
            needle = str(cond.get("text") or "")
            return any(needle in _event_text(ev).lower() for ev in job.events)
        if kind == "worker":
            label, state = str(cond.get("label") or ""), str(cond.get("state") or "")
            for name, st in (job.worker_states or {}).items():
                if label not in ("*", "any") and name != label:
                    continue
                if state in (st.get("seen") or ()):
                    return True
            return False
    except Exception as e:  # noqa: BLE001 - a predicate on a hot path never raises
        logger.debug("dispatch %s: condition check failed: %s", job.id, e)
    return False


async def _changed_snapshot(job: DispatchJob) -> Optional[Dict[str, Tuple[int, int]]]:
    if not job.workspace:
        return None
    try:
        snap, _trunc = await asyncio.to_thread(_snapshot, job.workspace)
        return snap
    except Exception as e:  # noqa: BLE001
        logger.debug("dispatch %s: changed-snapshot failed: %s", job.id, e)
        return None


def _wait_answer(job: DispatchJob, cond: Dict[str, Any], met: bool, started: float) -> Dict[str, Any]:
    return {"met": bool(met), "condition": str(cond.get("raw") or "done"),
            "waited_s": round(max(0.0, time.monotonic() - started), 3), "state": compact(job)}


async def wait_for(job: Any, *, condition: str = "done", timeout_s: float = 120.0) -> Dict[str, Any]:
    """Block until `condition` holds for this job, and no longer.

    `job` is a job id or a :class:`DispatchJob`. Returns
    ``{"met", "condition", "waited_s", "state"}`` — `state` being the same
    compact job answer `GET /api/dispatch/{id}` gives. A timeout is NOT an
    error: it answers ``met: False`` with how long it waited, so a coordinator
    can decide whether to wait again.

    It resolves the moment the condition becomes true, because the waiter
    sleeps on an :class:`asyncio.Event` the job's own progress path sets
    (:meth:`DispatchJob._wake`) — there is no poll tick inside. The one
    exception is `changed`, which cannot be pushed by a job at all: it re-reads
    the workspace when the job reports something, at most once every
    ``_CHANGED_SCAN_MIN_INTERVAL_S``.

    A condition that can no longer become true (anything but `done` on a job
    that has finished) returns at once with ``met: False`` instead of holding
    the caller until its timeout.

    `worker:<label>:<state>` reads the states `agent_worker_state_detection`
    records; with that setting off no worker ever enters one, so such a wait
    answers `met: False` exactly as it would have before the setting existed.
    """
    started = time.monotonic()
    cond = parse_condition(condition)
    target = job if isinstance(job, DispatchJob) else get(str(job or ""))
    if target is None:
        raise ValueError(f"no such dispatch job: {str(job)[:80]}")
    try:
        limit = max(0.0, float(timeout_s))
    except (TypeError, ValueError):
        limit = 120.0
    deadline = started + limit
    baseline = await _changed_snapshot(target) if cond["kind"] == "changed" else None
    if cond["kind"] == "changed" and baseline is None:
        return _wait_answer(target, cond, False, started)   # no workspace to watch
    changed = False
    scanned_at = -_CHANGED_SCAN_MIN_INTERVAL_S
    while True:
        # Subscribe BEFORE testing: an update between the test and the sleep
        # then wakes us instead of being lost.
        waiter = target.subscribe()
        try:
            due = True
            if cond["kind"] == "changed":
                due = (time.monotonic() - scanned_at) >= _CHANGED_SCAN_MIN_INTERVAL_S
                if due and baseline is not None:
                    scanned_at = time.monotonic()
                    now = await _changed_snapshot(target)
                    changed = now is not None and now != baseline
            if condition_holds(target, cond, changed):
                return _wait_answer(target, cond, True, started)
            if cond["kind"] != "done" and target.status not in _LIVE and due:
                return _wait_answer(target, cond, False, started)   # it never can now
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _wait_answer(target, cond, False, started)
            if not due:
                remaining = min(remaining, _CHANGED_SCAN_MIN_INTERVAL_S)
            try:
                await asyncio.wait_for(waiter.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
        finally:
            target.unsubscribe(waiter)


def new_events(job: DispatchJob, sent: int) -> Tuple[List[Dict[str, Any]], int]:
    """The events appended since a stream last read (and the new watermark).

    The deque rotates at EVENTS_KEPT, so a slow client can miss the oldest —
    it is told the newest instead of being told nothing, and no sequence
    number is stamped onto the events themselves (the plain events answer
    stays exactly what it was).
    """
    produced = int(getattr(job, "events_produced", 0) or 0)
    try:
        seen = int(sent)
    except (TypeError, ValueError):
        seen = 0
    if seen >= produced:
        return [], produced
    n = min(produced - seen, len(job.events))
    return (list(job.events)[-n:] if n > 0 else []), produced


def stream_end(job: DispatchJob, reason: str = "") -> Dict[str, Any]:
    """The `end` event of a live stream: the verdict the job settled on."""
    out = {"id": job.id, "status": job.status, "verdict": job.verdict or "", "error": job.error or ""}
    if reason:
        out["reason"] = reason
    return out


def cancel(job: DispatchJob) -> bool:
    """Stop the job. The status is `cancelling` until the worker tasks have
    unwound (their commands killed, the transcripts saved) and `_run`'s
    finally has recorded what changed on disk; a waiter returns then, never
    while the workspace is still being written."""
    if job.task is not None and not job.task.done():
        if not job._entered:
            # cancelled before its first step: _run never runs its finally
            job.status = "cancelled"
            job.verdict = "cancelled before it started"
            job.finished = time.time()
            job.task.cancel()
            _record_turn(job)
            job._persist()
            job._notify()
            return True
        job.status = "cancelling"
        job._event(event="job", message="cancelling")
        job.task.cancel()
        job._persist()
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
   `parallel: false` (they run in order, and a later task may edit what an
   earlier one wrote). Jobs in the same workspace run one at a time.
5. `workspace` is required: the folder the workers are confined to.
6. `verify` is the command that proves the job is done (`pytest -q`,
   `npm test`, `make check`…). Faustus runs it ITSELF after the workers, in
   the workspace — the workers' own claims are never the proof. Without it
   the project's test runner is auto-detected (`verify: "auto"`);
   `verify_scope: "all"` runs the whole suite instead of the tests related
   to the changed files. `fix_rounds` (default 1, max 2 — 4 while the
   convergence detector is on) is a MAXIMUM: when the verification fails,
   one fixer worker gets the failure output and the verification runs again,
   and the loop stops by itself as soon as the rounds stop changing anything
   (`result.convergence`, `result.stopped_by: "convergence"`).

## Reading the result
`status`: `done` = every worker finished AND the verification passed (or
could not run — read `verification.summary`); `partial` = some worker ended
`error` / `timeout` / `stalled` / `stopped`, or the verification failed;
`error` = nothing ran; `cancelled` / `interrupted` = stopped early (the
evidence is still there). `verdict` says it in one line.
`result.changes` is what Faustus SAW change on disk (checkpoint diff) —
`result.files_changed` is that list; `result.claimed_only` names files a
worker said it changed but did not. `result.verification` is the run Faustus
made: `ok`, `summary`, `failures`, `pre_existing` (failed before the job
too), `command`, `output_tail`; `attempts` > 1 means a fix round ran.
Per worker: status, files it claims, tool/round/token counts, its `outcome`
(`success` / `expected_error` / `cancelled` / `panic` — a worker the user
stopped is `cancelled`, not a failure) and its last words (≤ 1200 chars) —
never the transcript. Trust `changes` + `verification` over the prose.
A `running` answer carries `progress` per worker, `phase`, `ceiling_s` (the
most it can still take) and `wait_again: true` — call `workers_wait` again;
do NOT re-dispatch the same task because one wait returned early.
`result.proof` is the step after that: what the job can actually SHOW.
`verdict` is `proved` (the verification passed and every claimed file really
changed), `partial` (something is unaccounted for), `unproved` (nothing ran
that could show it) or `contradicted` (the disk or the tests say otherwise),
with a `confidence` and `uncertainty`, a NAMED reason for every point it is
missing. `unproved` is NOT a failure and NOT a success: report it as "the work
may have happened and nothing here can show it", and give the next job a
`verify` command so it can. Never report a job as done on `unproved` or
`contradicted`.
`task_order` appears when Faustus ran a sequential job's tasks in a different
order from the one you sent: the objectives they name are ranked by the
project's dependency graph and the highest-impact one goes first. `from` and
`to` say what moved; tasks naming no objective never move.
A worker's `progress` entry may also carry `state` and `why`: what its OWN
output says about it — `rate_limited`, `waiting_for_input`, `stuck`,
`auth_error`, `disk_full`, `oom` — with the literal that proves it. Such a
worker is reported, never killed: read `why`, and fix the cause (raise the
quota, answer the prompt in the board's chat, free the disk) instead of
re-dispatching the same task.

## Using an agent Faustus did not write
A task may name a `runner` (`{"instruction": "…", "runner": "claude"}`, or
job-wide `"runner": "opencode"`): the agent with that key does the work instead
of a local sub-agent. `GET /api/agent-runners` lists what this machine has,
with the licence word and whether each one can be a worker at all. Everything
above still applies — the checkpoint, the diff, `claimed_only`, Faustus's own
verification, the proof — with ONE difference you must pass on to the user:
**Faustus's command guard cannot see inside another agent's own shell.** No
destructive-command guard, no approval card, no file locks; only what changed
on disk afterwards. Such a job says so in its `verdict` and carries
`external_agent_unguarded` in `result.proof.uncertainty`. The feature is off by
default; a job naming a runner that is off, unknown or not installed is refused
with the reason and nothing starts.

## Waiting for something other than the end
`workers_wait_for(job_id, condition, timeout_s)` blocks until ONE condition
holds and returns the moment it does: `done` (the default, same as
`workers_wait`), `phase:<name>` (the job reaches a phase, e.g.
`phase:verification`), `worker:<label>:<state>` (a worker enters one of the
states above; `*` for any worker), `event:<text>` (any board event contains
it) or `changed` (anything changed on disk). `met: false` means the timeout
ran out, not that anything went wrong — wait again or read the status.

## Loop
plan → dispatch → wait (again if still running) → read verdict + changes +
verification → (dispatch a narrower fix if needed) → answer the user.
Do not re-do a worker's work yourself; send a narrower task instead. Tell the
user which changes came from the workers and point them at the board
(`chat_url`) if they want the details.
"""


def reset_for_tests() -> None:
    global _loaded_all_at
    _jobs.clear()
    _idempotent.clear()
    _running_ws.clear()
    _ws_waiters.clear()
    _loaded_all_at = 0.0
