"""When the proposer runs: after a task, in the background, and only if allowed.

``maybe_queue_after_turn`` is what the chat pipeline calls once a turn has been
saved. It is cheap and synchronous on purpose:

1. ``harness_refinement_enabled`` is off by default; off means a return before
   anything else happens.
2. When on, the turn is queued (one entry per session, bounded) and a single
   daemon worker thread takes entries one at a time. The foreground path never
   waits on a model, a database scan or a lock.
3. The worker first runs the deterministic pre-filter (no model): a task that
   went fine ends here. Only a trajectory with a real signal goes on to wait
   for admission.
4. Admission is the same set of probes the skills sleep pass uses before it
   spends a model call: unattended-failure breaker, foreground activity,
   ``background_job_guard.should_run_with_model`` (never loads a model on its
   own), model busy / no free slot, and a sibling instance holding the runner
   (``model_lease``). "Not now" is retried for a while and then dropped with a
   log line; it never forces a model into memory and never blocks the person.

The worker thread has its own event loop, so it does not depend on the loop of
the request that queued it (which may already be closing).
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from src.harness_refinement import proposer, signals, store

logger = logging.getLogger(__name__)

SETTING = "harness_refinement_enabled"
QUEUE_MAX = 16
#: Seconds to wait before looking at a turn: its messages and the chat stream
#: need a moment to settle.
SETTLE_S = 3.0
#: How long a queued job keeps retrying "not now" before it is dropped.
ADMISSION_WAIT_S = 600.0
ADMISSION_POLL_S = 5.0
SIBLING_ACTIVE_S = 120

_STATS: Dict[str, int] = {"queued": 0, "dropped_full": 0, "prefiltered": 0, "ran": 0, "deferred_dropped": 0}
_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=QUEUE_MAX)
_QUEUED_SESSIONS: set = set()
_STATE_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
_BUSY_SESSION: Optional[str] = None


def enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting(SETTING, False))
    except Exception:  # noqa: BLE001 - settings trouble means "off", never "on"
        return False


def status() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {"enabled": enabled(), "queued": _QUEUE.qsize(), "running": _BUSY_SESSION,
                "stats": dict(_STATS)}


# -- admission --------------------------------------------------------------

def _guard():
    from src import background_job_guard
    return background_job_guard


def admission_blocker(url: Optional[str] = None, model: Optional[str] = None, *,
                      guard: Any = None) -> Optional[str]:
    """None when an unattended model call may start now; otherwise a short,
    stable reason. Every probe failing to answer is treated as "do not run",
    except the optional ones (lease), which only add a reason when they know."""
    try:
        from src import unattended_breaker
        if unattended_breaker.is_open():
            return "unattended_breaker_open"
    except Exception:  # noqa: BLE001
        logger.debug("harness refinement: breaker probe failed", exc_info=True)
    try:
        from src.interactive_gate import has_foreground_activity
        if has_foreground_activity():
            return "foreground_activity"
    except Exception:  # noqa: BLE001
        logger.debug("harness refinement: foreground probe failed", exc_info=True)
    if not url or not model:
        resolved = proposer.resolve_endpoint()
        url, model = resolved
    if not url or not model:
        return "no_endpoint"
    try:
        g = guard if guard is not None else _guard()
        if not g.should_run_with_model(proposer.PURPOSE, url, model, user_initiated=False):
            return "would_load_model"
        if g.model_busy(url) or g.no_free_slot(url):
            return "model_busy"
    except Exception:  # noqa: BLE001
        logger.debug("harness refinement: model guard failed", exc_info=True)
        return "model_guard_error"
    try:
        from src import model_lease, vram_admission
        if model_lease.enabled():
            root = vram_admission.ollama_root(url)
            if root:
                if model_lease.sibling_reserved_bytes(root) > 0:
                    return "lease_reserved"
                now = time.time()
                if any(now - ts < SIBLING_ACTIVE_S for ts in model_lease.sibling_active(root).values()):
                    return "lease_sibling_active"
    except Exception:  # noqa: BLE001
        logger.debug("harness refinement: lease probe failed", exc_info=True)
    return None


# -- queueing ---------------------------------------------------------------

def maybe_queue_after_turn(session_id: str, owner: Optional[str] = None, *, enabled_fn: Optional[Callable[[], bool]] = None,
                           start_worker: bool = True) -> bool:
    """Called from the chat pipeline after a turn is saved. True when a job was
    queued. Never raises, never blocks."""
    try:
        if not (enabled_fn or enabled)():
            return False
        if not session_id:
            return False
        with _STATE_LOCK:
            if session_id in _QUEUED_SESSIONS or session_id == _BUSY_SESSION:
                return False
            job = {"session_id": session_id, "owner": owner or "", "queued_at": time.time()}
            try:
                _QUEUE.put_nowait(job)
            except queue.Full:
                _STATS["dropped_full"] += 1
                return False
            _QUEUED_SESSIONS.add(session_id)
            _STATS["queued"] += 1
        if start_worker:
            _ensure_worker()
        return True
    except Exception:  # noqa: BLE001
        logger.debug("harness refinement: queueing failed", exc_info=True)
        return False


def _ensure_worker() -> None:
    global _WORKER
    with _STATE_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _WORKER = threading.Thread(target=_worker_loop, name="harness-refinement", daemon=True)
        _WORKER.start()


def _worker_loop() -> None:
    global _BUSY_SESSION
    while True:
        try:
            job = _QUEUE.get(timeout=120)
        except queue.Empty:
            return                       # idle: the next queued turn starts a new worker
        with _STATE_LOCK:
            _QUEUED_SESSIONS.discard(job["session_id"])
            _BUSY_SESSION = job["session_id"]
        try:
            asyncio.run(run_job(job))
        except Exception:  # noqa: BLE001
            logger.debug("harness refinement job failed", exc_info=True)
        finally:
            with _STATE_LOCK:
                _BUSY_SESSION = None


async def run_job(job: Dict[str, Any], *, sleep: Callable[[float], Any] = asyncio.sleep,
                  admission: Optional[Callable[[Optional[str], Optional[str]], Optional[str]]] = None,
                  llm: Any = None, settle_s: float = SETTLE_S, wait_s: float = ADMISSION_WAIT_S,
                  poll_s: float = ADMISSION_POLL_S) -> Dict[str, Any]:
    """One queued turn: settle, pre-filter without a model, wait for admission,
    propose. Returns the proposer's result (or a ``skipped`` one)."""
    session_id, owner = job["session_id"], job.get("owner") or ""
    if settle_s:
        await sleep(settle_s)
    traj = signals.load_trajectory(session_id, owner or None)
    verdict = signals.assess(traj)
    if not verdict.worth:
        with _STATE_LOCK:
            _STATS["prefiltered"] += 1
        return {"status": "skipped", "reason": verdict.reason}

    check = admission or admission_blocker
    waited = 0.0
    counted = False
    while True:
        blocker = check(None, None)
        while blocker and waited < wait_s:
            await sleep(poll_s)
            waited += poll_s
            blocker = check(None, None)
        if blocker:
            return _drop(session_id, owner, verdict.trigger, blocker, waited)
        if not counted:
            counted = True
            with _STATE_LOCK:
                _STATS["ran"] += 1
        result = await proposer.propose_for_session(
            session_id, owner=owner, trigger=verdict.trigger, user_initiated=False, llm=llm,
            admission=lambda url, model: check(url, model), log_blocked=False)
        # The guard is asked again once the endpoint is known, and traffic can
        # arrive in between: that is "not yet", not "never". Same wait budget.
        if result.get("status") == "skipped" and result.get("blocked"):
            if waited >= wait_s:
                return _drop(session_id, owner, verdict.trigger, str(result.get("reason") or ""), waited)
            await sleep(poll_s)
            waited += poll_s
            continue
        return result


def _drop(session_id: str, owner: str, trigger: str, blocker: str, waited: float) -> Dict[str, Any]:
    """Give up on one job after the whole wait budget: log it, never retry later."""
    with _STATE_LOCK:
        _STATS["deferred_dropped"] += 1
    store.log_event("skipped", trigger=trigger, result=blocker, session_id=session_id, owner=owner,
                    detail={"waited_s": waited})
    return {"status": "skipped", "reason": blocker}


def reset_for_tests() -> None:
    global _BUSY_SESSION
    with _STATE_LOCK:
        while True:
            try:
                _QUEUE.get_nowait()
            except queue.Empty:
                break
        _QUEUED_SESSIONS.clear()
        _BUSY_SESSION = None
        for key in _STATS:
            _STATS[key] = 0
