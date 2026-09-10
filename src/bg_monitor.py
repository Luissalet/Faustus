"""Always-on monitor that auto-continues the agent when a background job
(see src/bg_jobs.py) finishes.

Reliability is the whole point: completion → agent re-invocation must never
silently no-op. The monitor drains `bg_jobs.pending_followups()` every tick and
only calls `mark_followed_up()` AFTER the agent run succeeds — so a transient
failure is simply retried on the next tick. A timed-out/dead job still produces
a follow-up ("the job failed/timed out"), so the user always hears back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src import bg_jobs
from src.prompt_security import untrusted_context_message

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5
# The follow-up agent run is allowed a few rounds to actually continue the task
# (e.g. after `pip install` finishes, run the transcription).
_FOLLOWUP_MAX_ROUNDS = 12


# ── RAM/commit pressure guard (PERF-04) ─────────────────────────────────────
#
# 08-09-2026 the box did not run out of VRAM first, it ran out of *commit*
# memory: two 27B models spilling into system RAM while an embeddings pass
# and a research run kept drawing on it too. Nothing watched that number, so
# nothing backed off before the cascade. This adds a watcher: `ram_pressure()`
# reads free/total RAM through an injectable reader (a test supplies a fake
# one — no psutil, no real host state needed), and the follow-up loop below
# consults it every tick before starting an auxiliary continuation, exactly
# the way it already consults `_accepting` during shutdown.
#
# State is conserved for free: a job skipped for pressure keeps
# `followed_up=False` in bg_jobs' own store, the same flag a shutdown-
# interrupted job leaves set, so the next tick (once pressure clears) simply
# picks it up rather than losing it. `_paused_jobs` additionally remembers
# *which* jobs are parked and since when, so a status view can show it rather
# than the pause being invisible.

RAM_PRESSURE_HIGH = 0.90   # fraction of total RAM in use: stop starting new auxiliary work
RAM_PRESSURE_LOW = 0.80    # fraction: safe to resume (hysteresis — see is_paused_for_resource_pressure)

MemoryReader = Callable[[], Tuple[Optional[int], Optional[int]]]


def _default_memory_reader() -> Tuple[Optional[int], Optional[int]]:
    """(available_bytes, total_bytes) from psutil. Optional dependency: a box
    without it reads as "unknown", never as "critical" (see ram_pressure)."""
    import psutil
    vm = psutil.virtual_memory()
    return int(vm.available), int(vm.total)


_memory_reader: MemoryReader = _default_memory_reader
_paused_for_pressure = False
_paused_jobs: Dict[str, Dict[str, Any]] = {}


def set_memory_reader(reader: Optional[MemoryReader]) -> None:
    """Test hook: inject a fake `() -> (available_bytes, total_bytes)` reader.
    Pass None to restore the real psutil-backed one."""
    global _memory_reader
    _memory_reader = reader or _default_memory_reader


def ram_pressure() -> Dict[str, Any]:
    """One reading of system memory pressure. `used_fraction` is None when the
    reader fails or reports no total — that is "unknown", and `critical` is
    always False for it: ignorance is never a reason to pause every auxiliary
    job in the system."""
    try:
        available, total = _memory_reader()
    except Exception as e:  # noqa: BLE001
        logger.debug("bg-monitor: memory reader failed: %s", e)
        return {"available_bytes": None, "total_bytes": None, "used_fraction": None, "critical": False}
    if not total:
        return {"available_bytes": available, "total_bytes": total, "used_fraction": None, "critical": False}
    used_fraction = max(0.0, 1.0 - (float(available or 0) / float(total)))
    return {"available_bytes": available, "total_bytes": total, "used_fraction": used_fraction,
            "critical": used_fraction >= RAM_PRESSURE_HIGH}


def is_paused_for_resource_pressure() -> bool:
    """True while auxiliary work should stand down for RAM/commit pressure.

    Hysteresis, not a bare threshold: once tripped at `RAM_PRESSURE_HIGH` it
    stays tripped until usage falls back under `RAM_PRESSURE_LOW`, so a
    reading bouncing around one number does not pause and resume the same job
    every five-second tick."""
    global _paused_for_pressure
    frac = ram_pressure()["used_fraction"]
    if frac is None:
        return _paused_for_pressure  # unknown: hold the last known state, don't guess
    if frac >= RAM_PRESSURE_HIGH:
        _paused_for_pressure = True
    elif frac <= RAM_PRESSURE_LOW:
        _paused_for_pressure = False
    return _paused_for_pressure


def pressure_state() -> Dict[str, Any]:
    """The guard's own snapshot — current reading, whether it is paused, and
    which auxiliary jobs are parked because of it. For a status endpoint or
    the Vitals dialog; not consumed anywhere in this lot's own routes."""
    state = ram_pressure()
    state["paused"] = is_paused_for_resource_pressure()
    state["paused_jobs"] = list(_paused_jobs.values())
    return state


def note_paused(job_id: str, kind: str = "") -> None:
    """Record that `job_id` (an auxiliary job — a background continuation
    here; embeddings/audit/research callers elsewhere can reuse the same
    predicate, see the lot report) was skipped this tick for RAM pressure."""
    if job_id not in _paused_jobs:
        logger.info("bg-monitor: pausing auxiliary job %s (%s) for RAM pressure", job_id, kind or "job")
    _paused_jobs[job_id] = {"job_id": job_id, "kind": kind, "paused_at": time.time()}


def note_resumed(job_id: str) -> None:
    if _paused_jobs.pop(job_id, None) is not None:
        logger.info("bg-monitor: resuming auxiliary job %s — RAM pressure cleared", job_id)


def paused_job_ids() -> List[str]:
    return list(_paused_jobs.keys())

# Shutdown contract (B-013). A tick re-invokes the agent, which appends to a
# session and calls save_sessions(); one that starts after the process has begun
# shutting down writes through a SessionManager whose dependencies are already
# being closed. So stopping is a gate, checked at the top of the tick AND
# between individual follow-ups, rather than a bare cancel: cancelling inside
# `_run_followup` would abort an agent run at an arbitrary point, and since a
# job is marked followed_up only after the run completes, an aborted one is
# simply retried on the next boot -- which is the right outcome, but only if
# nothing was half-written first. Hence the three verbs: `stop_accepting` (no
# new follow-ups), `drain` (wait, bounded, for the one in flight), `close`
# (cancel what is left). `_stop` doubles as the poll sleep's wakeup so shutdown
# does not wait out a whole POLL_INTERVAL_S. It is created per run, never at
# import: an asyncio.Event binds itself to the loop that first waits on it, and
# the second lifespan in a process (tests, `uvicorn --reload`) runs on a new
# one, where a module-level Event raises "bound to a different event loop" from
# inside the poll sleep and kills the monitor on the spot.
_accepting = True
_stop = None


def _background_result_message(rec):
    inject = (
        f"[Background job {rec['id']} finished]\n\n"
        f"{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Don't repeat work that's already done. "
        "If the task is now complete, give the user the final result."
    )
    return untrusted_context_message("background job output", inject)


async def _drain_agent(sess, messages):
    """Run the agent loop headless against a session. Returns
    (final_prose, tool_events) — tool_events in the same shape the live chat
    saves, so the frontend rebuilds them as standard agent-thread tool cards."""
    from src.agent_loop import stream_agent_loop
    full = ""
    tool_events = []
    round_num = 1
    async for chunk in stream_agent_loop(
        sess.endpoint_url, sess.model, messages,
        headers=getattr(sess, "headers", None),
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=_FOLLOWUP_MAX_ROUNDS,
        owner=getattr(sess, "owner", None),
    ):
        if not chunk.startswith("data: "):
            continue
        body = chunk[6:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        if not isinstance(d, dict):
            continue
        if "delta" in d:
            delta = d.get("delta")
            if isinstance(delta, str):
                if d.get("thinking"):
                    continue
                full += delta
        elif d.get("type") == "agent_step":
            round_num = d.get("round", round_num)
        elif d.get("type") == "tool_output":
            # Mirror the live chat's tool_event shape (chat_routes / chatRenderer).
            tool_event = {
                "round": round_num,
                "tool": d.get("tool"),
                "command": d.get("command"),
                "output": d.get("output"),
                "exit_code": d.get("exit_code"),
            }
            if isinstance(d.get("ask_user"), dict):
                # Preserve exact-approval cards from a tainted background-job
                # continuation so the user can authorize the sealed action on
                # the next foreground turn instead of losing it headlessly.
                tool_event["ask_user"] = d["ask_user"]
            tool_events.append(tool_event)
    return full, tool_events


async def _run_followup(rec: dict) -> bool:
    """Re-invoke the agent in the job's session with the result. Returns True
    if the follow-up completed (or there's nothing to do) — i.e. it's safe to
    mark followed_up. Returns False to retry on the next tick."""
    from src.ai_interaction import get_session_manager
    from core.models import ChatMessage

    sm = get_session_manager()
    if not sm:
        return False  # not ready yet — retry
    sess = sm.get_session(rec["session_id"])
    if not sess:
        # Session was deleted — nothing to continue. Consider it handled so we
        # don't retry forever.
        logger.info("bg-followup: session %s gone for job %s — skipping", rec.get("session_id"), rec.get("id"))
        return True

    # Don't write into a session that's mid-stream. The followup appends to
    # history + save_sessions(); a concurrent live turn does the same, and with
    # no per-session lock the two interleave (reordered/clobbered messages).
    # Defer — return False so we retry on the next tick once the turn finishes.
    # `active_session_ids()` covers detached runs AND externally-marked busy
    # sessions (sub-agent workers, and the follow-up below), so two writers on
    # one session can no longer slip past each other.
    agent_runs = None
    try:
        from src import agent_runs
        if sess.id in agent_runs.active_session_ids():
            logger.info("bg-followup: session %s busy (live turn) — deferring job %s", sess.id, rec.get("id"))
            return False
    except Exception:
        agent_runs = None

    # Announce ourselves for the length of the run: the check above is only
    # half a mutex if nobody ever sets the flag.
    if agent_runs is not None:
        try:
            agent_runs.mark_busy(sess.id)
        except Exception:
            agent_runs = None
    try:
        context = sess.get_context_messages()
        context.append(_background_result_message(rec))

        full, tool_events = await _drain_agent(sess, context)

        # Persist ONLY the assistant continuation so it renders as a normal agent
        # turn — a standard chat bubble plus `tool_events` that the frontend
        # rebuilds into the usual agent-thread tool cards (chatRenderer:1494). The
        # trigger isn't saved as its own message (it'd be an out-of-place bubble);
        # the raw job output is stashed in metadata for traceability instead.
        sm.add_message(sess.id, ChatMessage(
            "assistant", full,
            metadata={
                "tool_events": tool_events,
                "model": sess.model,
                "bg_job_id": rec["id"],
                "bg_result": bg_jobs.result_text(rec)[:4000],
            },
        ))
        sm.save_sessions()
    finally:
        if agent_runs is not None:
            try:
                agent_runs.clear_busy(sess.id)
            except Exception:
                pass
    logger.info("bg-followup: auto-continued session %s for job %s (%d chars, %d tools)",
                sess.id, rec["id"], len(full), len(tool_events))
    return True


async def _loop(stop):
    while not stop.is_set():
        try:
            pressured = is_paused_for_resource_pressure()
            for rec in bg_jobs.pending_followups():
                if not _accepting:
                    # Shutdown began mid-tick. The remaining jobs keep
                    # followed_up=False, so the next boot picks them up rather
                    # than losing them: that flag is the whole retry contract.
                    break
                if pressured:
                    # RAM/commit pressure (PERF-04): pause this continuation
                    # BEFORE it starts a new agent round (more tool calls, more
                    # subprocess/embedding work) rather than after the box is
                    # already tight. followed_up stays False — the exact same
                    # conservation the shutdown branch above relies on — so
                    # this job resumes on its own once pressure clears instead
                    # of being lost.
                    note_paused(rec["id"], kind="bg_job_followup")
                    continue
                note_resumed(rec["id"])
                try:
                    if await _run_followup(rec):
                        bg_jobs.mark_followed_up(rec["id"])
                except Exception as e:
                    # Idempotent: leave followed_up=False so the next tick retries.
                    logger.warning("bg-followup failed for %s (will retry): %s", rec.get("id"), e)
        except Exception as e:
            logger.warning("bg-monitor tick error: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            continue
    logger.info("Background-job monitor stopped")


def start_bg_monitor():
    """Idempotent — start the always-on background-job monitor."""
    global _monitor_task, _accepting, _stop
    # Re-arm before the liveness check: a second lifespan in one process (tests,
    # `uvicorn --reload`) inherits the module-level flags a previous shutdown
    # left set, and a monitor that starts already-stopped is a silent no-op.
    _accepting = True
    if _monitor_task and not _monitor_task.done():
        if _stop is not None:
            _stop.clear()
        return _monitor_task
    _stop = asyncio.Event()
    _monitor_task = asyncio.create_task(_loop(_stop))
    logger.info("Background-job monitor started (poll %ds)", POLL_INTERVAL_S)
    return _monitor_task


def stop_accepting() -> None:
    """Start no further follow-ups, and wake the poll sleep. Idempotent."""
    global _accepting
    _accepting = False
    if _stop is not None:
        _stop.set()


async def drain(deadline: float) -> bool:
    """Wait until `deadline` (a `time.monotonic()` stamp) for the loop to finish
    the follow-up it is inside and exit. True when it did."""
    task = _monitor_task
    if task is None or task.done():
        return True
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    done, _pending = await asyncio.wait([task], timeout=remaining)
    return bool(done)


async def close() -> None:
    """Cancel the loop and await the cancellation. Idempotent, and safe to call
    without ever having started the monitor."""
    global _monitor_task
    stop_accepting()
    task, _monitor_task = _monitor_task, None
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug("bg-monitor shutdown error: %s", e)
