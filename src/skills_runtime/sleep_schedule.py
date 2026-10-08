"""src/skills_runtime/sleep_schedule.py - runs the skill sleep pass by itself at night.

`sleep_optimize` mines recent sessions for evidence about a skill and stores
ONE proposed SKILL.md revision per call. Until now that only ran on demand
(the "Proposals" tab, `POST /api/skills/{id}/sleep-pass`). This module is the
schedule: a SYSTEM loop, spawned by `app.py` next to the nightly skills audit
(`skill-audit-nightly`) and shaped like it - it is not a user `ScheduledTask`,
so it never shows up in the Tasks list and cannot be edited or deleted by
accident. It only ever PROPOSES: nothing it produces reaches a SKILL.md
without the human approval `sleep_optimize.approve_proposal` already demands.

Settings (both live-read, so no restart)
----------------------------------------
* `skills_sleep_pass_enabled` (default False): off means the loop does
  nothing at all, so behaviour for existing installs is unchanged.
* `skills_sleep_pass_hour` (default 3, 0-23, local time): the hour of day the
  pass becomes due.

When it runs
------------
* At most ONE scheduled run per local day. The day is identified by the date of
  the slot (today at the configured hour, or yesterday's if the hour has not
  come yet today), saved in `DATA_DIR/skill_proposals/sleep_schedule.json`
  BEFORE the pass starts, so a restart or a crash in the middle never repeats
  the day. Changing the hour after the day's run does not run it again.
* Catch-up window: the pass is due from the slot until `CATCHUP_HOURS` (3)
  after it. A machine that was asleep or off at the hour still gets its pass
  when it wakes inside that window (the loop ticks every `TICK_SECONDS`);
  after the window the day is recorded as `missed` and the next slot is
  tomorrow's. Turning the setting on inside the window of today's slot runs it
  at the next tick.
* Never while something else holds the machine - checked before the pass and
  again before every skill, and a "not now" does NOT use up the day, it is
  retried on the next tick until the window closes:
    - another sleep pass is in progress (on demand from the UI/API, or a
      previous scheduled one), in this process or in ANOTHER Faustus process
      sharing the data directory - `track()` is the shared registry (see
      "Cross-process exclusion");
    - the unattended-failure breaker is open (`src.unattended_breaker`);
    - the person is working (`src.interactive_gate.has_foreground_activity`:
      a request, a browser tab, a chat stream, an agent run);
    - the model the pass would use is not already resident
      (`src.background_job_guard.should_run_with_model`, which respects the
      `background_jobs_may_load_models` opt-in) or is serving a request right
      now (`model_busy` / `no_free_slot`);
    - another Faustus instance sharing the runner holds a VRAM reservation or
      used the runner in the last `SIBLING_ACTIVE_S` seconds (`src.model_lease`).
* A pass whose every model call failed (endpoint down, no endpoint) is retried
  at the next tick too, at most `MAX_ATTEMPTS` times per slot.
* The guard is re-run immediately before EVERY proposal, the first one
  included (evidence collection can take a while). If it says "not now"
  before a single proposal was made, nothing was done: the day and the attempt
  are given back and the next tick tries again.

Cross-process exclusion
-----------------------
Two Faustus processes can share one data directory, so in-process locks are
not enough. Two small files under `skill_proposals/` coordinate them:

* `sleep_schedule.json.lock` - a `core.file_lock.FileLock` (O_EXCL create, stale
  takeover). Every read-decide-claim and every write of the state file happens
  under it, and the decision is re-taken under it, so the second process
  re-reads the day the first one already claimed and skips.
* `sleep_pass.running` - the run lease, created (O_EXCL) by whoever runs a pass,
  scheduled or on demand, naming its owner by pid AND process start time and
  removed when the pass ends. A lease whose owner is dead (or whose pid was
  reused) is taken over; one older than `MAX_LEASE_AGE_S` is ignored. The
  on-demand route takes the same lease through `track()` and answers 409 when
  another process holds it.

At-most-once, and what a crash leaves behind
--------------------------------------------
The day is claimed (`last_run_date`, `running_since`, `running_slot`) BEFORE
any work, so a scheduled day runs AT MOST ONCE per slot. Graceful outcomes give
the day back (shutdown during the pass, a deferral before any proposal, a pass
whose every model call failed, up to `MAX_ATTEMPTS`). A HARD crash (power cut,
kill -9) after the claim cannot give anything back: the next tick, or the
status API, finds `running_since` with no live owner and records the run as
`interrupted`, readable in `last_run`. That day is NOT retried blindly - a
half-finished pass may already have stored proposals - and the schedule simply
continues with the next slot.

What one pass does
------------------
It looks at the published skills of every owner, keeps those with at least
`MIN_EVIDENCE` consulted turns in the last `EVIDENCE_DAYS` days that include
at least one failure signal (a negative user reaction or a tool error), drops
those that already got a proposal in the last `COOLDOWN_DAYS` days (pending,
approved or rejected - a rejected one is not nagged again every night), ranks
the rest by failure signals and proposes for the top `MAX_SKILLS_PER_RUN`.
Each proposal is the ordinary stored `pending` record.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from core.atomic_io import atomic_write_json
from core.file_lock import FileLock, LockTimeout

from . import sleep_optimize

logger = logging.getLogger(__name__)

SETTING_ENABLED = "skills_sleep_pass_enabled"
SETTING_HOUR = "skills_sleep_pass_hour"
DEFAULT_HOUR = 3

#: How long after the configured hour a pass is still allowed to start.
CATCHUP_HOURS = 3
#: Seconds between two looks at the clock (the loop's whole cost when idle).
TICK_SECONDS = 300
#: Seconds the loop waits after startup before its first look.
STARTUP_DELAY_S = 90
#: Model-backed attempts a single slot may burn when every call fails.
MAX_ATTEMPTS = 3
MAX_SKILLS_PER_RUN = 3
MIN_EVIDENCE = 2
EVIDENCE_DAYS = 30
EVIDENCE_LIMIT = 20
COOLDOWN_DAYS = 7
#: Another instance that used the same runner this recently counts as busy.
SIBLING_ACTIVE_S = 120

_SKIP_STATUSES = frozenset({"draft", "archived", "disabled"})
_FATAL_MODEL_CLASSES = frozenset({"sleep_pass.model_call_failed", "sleep_pass.no_endpoint"})

#: How long a process may wait for the short state lock (read-decide-claim and
#: result writes are milliseconds; a timeout means something is wrong).
STATE_LOCK_TIMEOUT_S = 10.0
STATE_LOCK_STALE_S = 60.0
#: A run lease older than this is treated as abandoned even if its owner pid is
#: still alive (a hung process must not block the schedule forever).
MAX_LEASE_AGE_S = 6 * 3600

_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Dict[int, Dict[str, Any]] = {}
_ACTIVE_SEQ = 0
_LEASE_GUARD = threading.RLock()
_LEASE_REFS = 0


class SleepPassBusy(Exception):
    """Another process holds the run lease, or the state lock never freed."""

    def __init__(self, owner: Optional[Mapping[str, Any]] = None):
        super().__init__("a sleep pass is already running")
        self.owner = dict(owner or {})


# -- cross-process exclusion ------------------------------------------------
#
# Two files next to the proposals, both under `PROPOSALS_ROOT`:
#   sleep_schedule.json.lock - a short `core.file_lock.FileLock` taken around
#       every read-decide-claim and every result write of the state file.
#   sleep_pass.running - the run lease: created with O_EXCL by whoever runs a
#       pass (scheduled or on demand) and removed when it ends. It names the
#       owner by pid AND process start time, so a crashed owner (or a pid the
#       OS has since reused) is recognised as gone and the lease taken over.

def _lease_path() -> str:
    return os.path.join(sleep_optimize.PROPOSALS_ROOT, "sleep_pass.running")


def _state_lock() -> FileLock:
    return FileLock(_state_path() + ".lock", timeout=STATE_LOCK_TIMEOUT_S,
                    stale_after=STATE_LOCK_STALE_S)


def _process_start(pid: int) -> Optional[float]:
    try:
        import psutil
        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # noqa: BLE001 - no psutil / no such process
        return None


def _me() -> Dict[str, Any]:
    pid = os.getpid()
    return {"pid": pid, "pstart": _process_start(pid)}


def _same_process(owner: Mapping[str, Any], other: Mapping[str, Any]) -> bool:
    if owner.get("pid") != other.get("pid"):
        return False
    a, b = owner.get("pstart"), other.get("pstart")
    return a is None or b is None or abs(float(a) - float(b)) < 2.0


def _owner_alive(owner: Mapping[str, Any]) -> bool:
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        proc = psutil.Process(pid)
        started = owner.get("pstart")
        if started is not None and abs(float(proc.create_time()) - float(started)) >= 2.0:
            return False                       # the pid was reused by another process
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 - NoSuchProcess / AccessDenied
        try:
            import psutil
            return bool(psutil.pid_exists(pid))
        except Exception:  # noqa: BLE001
            return False
    try:
        from src import model_lease
        return bool(model_lease._pid_alive(pid))
    except Exception:  # noqa: BLE001
        return True


def read_lease() -> Optional[Dict[str, Any]]:
    try:
        with open(_lease_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lease_holder() -> Optional[Dict[str, Any]]:
    """The OTHER live process holding the run lease, if any. A lease owned by
    this very process, by a dead one or by one older than `MAX_LEASE_AGE_S` is
    not a holder."""
    lease = read_lease()
    if not lease:
        return None
    if _same_process(lease, _me()):
        return None
    try:
        if time.time() - float(lease.get("since") or 0) > MAX_LEASE_AGE_S:
            return None
    except (TypeError, ValueError):
        return None
    return lease if _owner_alive(lease) else None


def overlap_reason() -> Optional[str]:
    """`sleep_pass_running` when a pass is in progress in this process or in
    another one."""
    if _LEASE_REFS > 0 or active_runs() or lease_holder():
        return "sleep_pass_running"
    return None


def _lease_acquire(label: str, *, state_locked: bool, exclusive: bool = False) -> None:
    global _LEASE_REFS
    with _LEASE_GUARD:
        if _LEASE_REFS > 0:
            if exclusive:
                raise SleepPassBusy({**_me(), "label": "this_process"})
            _LEASE_REFS += 1
            return
        try:
            guard = contextlib.nullcontext() if state_locked else _state_lock()
            with guard:
                holder = lease_holder()
                if holder:
                    raise SleepPassBusy(holder)
                os.makedirs(sleep_optimize.PROPOSALS_ROOT, exist_ok=True)
                with contextlib.suppress(OSError):
                    os.remove(_lease_path())            # dead, own-leftover or expired
                try:
                    fd = os.open(_lease_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    raise SleepPassBusy(read_lease()) from None
                try:
                    os.write(fd, json.dumps({**_me(), "label": label,
                                             "since": time.time()}).encode("utf-8"))
                finally:
                    os.close(fd)
        except LockTimeout:
            raise SleepPassBusy({"reason": "state_lock_timeout"}) from None
        _LEASE_REFS = 1


def _lease_release() -> None:
    global _LEASE_REFS
    with _LEASE_GUARD:
        _LEASE_REFS = max(0, _LEASE_REFS - 1)
        if _LEASE_REFS == 0:
            lease = read_lease()
            if lease and _same_process(lease, _me()):
                with contextlib.suppress(OSError):
                    os.remove(_lease_path())


# -- shared "a pass is running" registry ------------------------------------

@contextlib.contextmanager
def track(label: str = "manual", *, _state_locked: bool = False,
          exclusive: bool = False) -> Iterator[None]:
    """Marks a sleep pass as in progress for the duration of the block, in this
    process (`active_runs`) and, through the run lease, in every other one.
    The on-demand route and the scheduled run both wrap their work in it,
    which is what stops them overlapping. Raises `SleepPassBusy` when another
    process already holds the lease, or (`exclusive=True`) when this one does."""
    global _ACTIVE_SEQ
    _lease_acquire(label, state_locked=_state_locked, exclusive=exclusive)
    with _ACTIVE_LOCK:
        _ACTIVE_SEQ += 1
        token = _ACTIVE_SEQ
        _ACTIVE[token] = {"label": label, "since": time.time()}
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(token, None)
        _lease_release()


def active_runs() -> List[Dict[str, Any]]:
    with _ACTIVE_LOCK:
        return [dict(v) for v in _ACTIVE.values()]


def reset_for_tests() -> None:
    global _LEASE_REFS
    with _ACTIVE_LOCK:
        _ACTIVE.clear()
    with _LEASE_GUARD:
        _LEASE_REFS = 0


# -- settings ---------------------------------------------------------------

def _get_setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings trouble must never kill the loop
        return default


def is_enabled() -> bool:
    return bool(_get_setting(SETTING_ENABLED, False))


def configured_hour() -> int:
    """The configured hour, 0-23. A missing or unusable value is the default;
    hour 0 (midnight) is a legitimate value and is kept."""
    raw = _get_setting(SETTING_HOUR, DEFAULT_HOUR)
    try:
        hour = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HOUR
    return hour if 0 <= hour <= 23 else DEFAULT_HOUR


# -- persisted state --------------------------------------------------------

def _state_path() -> str:
    return os.path.join(sleep_optimize.PROPOSALS_ROOT, "sleep_schedule.json")


def load_state() -> Dict[str, Any]:
    import json
    try:
        with open(_state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: Mapping[str, Any]) -> None:
    os.makedirs(sleep_optimize.PROPOSALS_ROOT, exist_ok=True)
    atomic_write_json(_state_path(), dict(state), indent=2)


# -- the clock --------------------------------------------------------------

def slot_for(now: datetime, hour: int) -> datetime:
    """The most recent moment at or before `now` that was `hour`:00 local."""
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    return slot


def next_slot_after(now: datetime, hour: int) -> datetime:
    slot = slot_for(now, hour)
    return slot + timedelta(days=1)


@dataclass(frozen=True)
class Decision:
    action: str            # "run" | "skip"
    reason: str            # "due" | "disabled" | "already_ran" | "outside_window"
    slot_date: str = ""    # YYYY-MM-DD of the slot this decision is about


def evaluate(now: datetime, *, enabled: bool, hour: int,
             last_run_date: Optional[str]) -> Decision:
    """Pure decision: is a scheduled pass due at `now`? No I/O, no clock."""
    if not enabled:
        return Decision("skip", "disabled")
    slot = slot_for(now, hour)
    slot_date = slot.date().isoformat()
    if last_run_date == slot_date:
        return Decision("skip", "already_ran", slot_date)
    if now - slot >= timedelta(hours=CATCHUP_HOURS):
        return Decision("skip", "outside_window", slot_date)
    return Decision("run", "due", slot_date)


# -- admission: is the machine free for a background model pass? ------------

def _resolve_pass_endpoint():
    from src.endpoint_resolver import resolve_endpoint
    return resolve_endpoint("skills_sleep_pass")


def admission_blocker(*, url: Optional[str] = None, model: Optional[str] = None,
                      ignore_running: bool = False) -> Optional[str]:
    """None when the pass may start; otherwise a short stable reason. Every
    probe is the existing one the other unattended jobs use, and each is
    wrapped: a probe that itself fails never blocks forever (the model-load
    guard, which is the one that matters for VRAM, fails closed on its own).
    `ignore_running` is for the checks a pass makes on ITSELF in the middle of
    its own run, when the pass in progress is that very pass."""
    if not ignore_running and overlap_reason():
        return "sleep_pass_running"
    try:
        from src import unattended_breaker
        if unattended_breaker.is_open():
            return "unattended_breaker_open"
    except Exception:  # noqa: BLE001
        logger.debug("sleep schedule: breaker probe failed", exc_info=True)
    try:
        from src.interactive_gate import has_foreground_activity
        if has_foreground_activity():
            return "foreground_activity"
    except Exception:  # noqa: BLE001
        logger.debug("sleep schedule: foreground probe failed", exc_info=True)

    if url is None or model is None:
        try:
            url, model, _headers = _resolve_pass_endpoint()
        except Exception:  # noqa: BLE001
            url, model = None, None
    if not url or not model:
        return "no_endpoint"
    try:
        from src import background_job_guard as guard
        if not guard.should_run_with_model("skills_sleep_pass", url, model, user_initiated=False):
            return "would_load_model"
        if guard.model_busy(url) or guard.no_free_slot(url):
            return "model_busy"
    except Exception:  # noqa: BLE001
        logger.debug("sleep schedule: model guard failed", exc_info=True)
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
        logger.debug("sleep schedule: lease probe failed", exc_info=True)
    return None


# -- picking skills ---------------------------------------------------------

def _failure_signals(evidence: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for e in evidence
               if e.get("user_reaction") == "negative" or e.get("tool_errors"))


def select_candidates(skills: Sequence[Mapping[str, Any]],
                      evidence_fn: Callable[..., List[Dict[str, Any]]],
                      proposals: Mapping[str, Mapping[str, Any]],
                      *, now_ts: float, limit: int = MAX_SKILLS_PER_RUN) -> List[Dict[str, Any]]:
    """The skills worth a proposal tonight, best first. `evidence_fn` has
    `sleep_optimize.collect_evidence`'s signature."""
    cooldown_cut = now_ts - COOLDOWN_DAYS * 86400
    recently: set = set()
    for rec in proposals.values():
        try:
            if float(rec.get("created_at") or 0) >= cooldown_cut:
                recently.add((str(rec.get("skill_id") or ""), str(rec.get("owner") or "")))
        except (TypeError, ValueError):
            continue
    ranked: List[Dict[str, Any]] = []
    seen: set = set()
    for sk in skills:
        name = str(sk.get("name") or "").strip()
        if not name or str(sk.get("status") or "").lower() in _SKIP_STATUSES:
            continue
        owner = str(sk.get("owner") or "").strip()
        owner = "" if owner == "*" else owner
        if (name, owner) in seen or (name, owner) in recently:
            continue
        seen.add((name, owner))
        try:
            evidence = evidence_fn(name, since_days=EVIDENCE_DAYS, limit=EVIDENCE_LIMIT,
                                   owner=owner or None)
        except Exception:  # noqa: BLE001 - one unreadable skill never stops the night
            logger.debug("sleep schedule: evidence failed for %s", name, exc_info=True)
            continue
        signals = _failure_signals(evidence)
        if len(evidence) < MIN_EVIDENCE or signals < 1:
            continue
        ranked.append({"skill_id": name, "owner": owner, "evidence": evidence,
                       "signals": signals, "score": signals * 3 + len(evidence)})
    ranked.sort(key=lambda c: (-c["score"], c["skill_id"]))
    return ranked[:max(0, int(limit))]


# -- running ----------------------------------------------------------------

@dataclass
class Deps:
    """Everything the scheduler touches outside this file, so tests can
    replace the clock, the skills, the evidence, the model and the probes."""
    now: Callable[[], datetime] = datetime.now
    skills: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None
    evidence: Callable[..., List[Dict[str, Any]]] = sleep_optimize.collect_evidence
    propose: Callable[..., Awaitable[Dict[str, Any]]] = sleep_optimize.propose
    blocker: Callable[..., Optional[str]] = admission_blocker
    endpoint: Callable[[], Any] = _resolve_pass_endpoint
    in_thread: bool = True


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _default_skills(skills_manager) -> Callable[[], Sequence[Mapping[str, Any]]]:
    return lambda: skills_manager.load(owner=None)


async def _run_pass(deps: Deps, slot_date: str, started: datetime) -> Dict[str, Any]:
    """The body of one scheduled pass. Returns the stored result record."""
    result: Dict[str, Any] = {
        "trigger": "scheduled", "slot_date": slot_date, "started_at": _iso(started),
        "status": "done", "considered": 0, "candidates": 0,
        "proposals": [], "errors": [], "stopped": "",
    }
    skills = list(deps.skills() if deps.skills else [])
    result["considered"] = len(skills)

    def _select():
        return select_candidates(skills, deps.evidence, sleep_optimize.load_proposals(),
                                 now_ts=time.time())

    # Evidence is a database scan per skill: keep it off the event loop.
    candidates = await asyncio.to_thread(_select) if deps.in_thread else _select()
    result["candidates"] = len(candidates)
    if not candidates:
        result["status"] = "nothing_to_do"
        return result

    url, model, _headers = deps.endpoint()
    model_failures = 0
    for index, cand in enumerate(candidates):
        # Revalidate immediately before EVERY proposal, the first one included:
        # evidence collection above can take a while and the person may have
        # sat down meanwhile. A "not now" before any proposal is work not done.
        blocker = deps.blocker(url=url, model=model, ignore_running=True)
        if blocker:
            result["stopped"] = blocker
            result["no_work"] = index == 0
            break
        try:
            record = await deps.propose(
                cand["skill_id"], cand["evidence"], url=url, model=model,
                owner=cand["owner"] or None)
            result["proposals"].append(
                {"id": record.get("id"), "skill_id": cand["skill_id"]})
        except sleep_optimize.SleepPassError as exc:
            result["errors"].append({"skill_id": cand["skill_id"], "error_class": exc.error_class})
            if exc.error_class in _FATAL_MODEL_CLASSES:
                model_failures += 1
                result["stopped"] = exc.error_class
                break
        except Exception as exc:  # noqa: BLE001
            result["errors"].append({"skill_id": cand["skill_id"],
                                     "error_class": f"unexpected.{type(exc).__name__}"})
    if not result["proposals"] and model_failures:
        result["status"] = "failed"
        result["retry"] = True          # endpoint trouble: worth another tick
    elif result["stopped"] and result["stopped"] not in _FATAL_MODEL_CLASSES:
        result["status"] = "partial" if result["proposals"] else "deferred"
        result["retry"] = not result["proposals"]
    elif result["errors"] and not result["proposals"]:
        result["status"] = "failed"     # validation refusals: retrying would repeat them
    if not result.get("no_work"):
        result.pop("no_work", None)
    return result


def _update_state(apply: Callable[[Dict[str, Any]], None]) -> bool:
    """Read-modify-write of the state file under the cross-process lock.
    False (and a warning) when the lock never freed - never raises."""
    try:
        with _state_lock():
            state = load_state()
            apply(state)
            save_state(state)
        return True
    except LockTimeout:
        logger.warning("skills sleep pass: state lock timeout, update skipped")
        return False


def _settle_locked(state: Dict[str, Any], now: datetime) -> bool:
    """`running_since` with no live owner is a hard crash (power cut, kill -9)
    after the day was claimed. Record it as an `interrupted` last result and
    clear the marker; `last_run_date` is left alone, so the day is NOT run a
    second time (at-most-once). The caller holds the state lock."""
    started = state.get("running_since")
    if not started or overlap_reason():
        return False
    slot = str(state.get("running_slot") or state.get("last_run_date") or "")
    state["last_result"] = {
        "trigger": "scheduled", "slot_date": slot, "started_at": started,
        "finished_at": _iso(now), "status": "interrupted", "considered": 0,
        "candidates": 0, "proposals": [], "errors": [], "stopped": "",
    }
    state["last_run_at"] = _iso(now)
    state.pop("running_since", None)
    state.pop("running_slot", None)
    logger.warning("skills sleep pass: the run for %s was interrupted (process died); "
                   "the day is not repeated", slot)
    return True


def _settle_interrupted(now: datetime) -> None:
    _update_state(lambda state: _settle_locked(state, now))


def _note_deferred(now: datetime, reason: str, slot_date: str) -> None:
    def apply(state: Dict[str, Any]) -> None:
        last = state.get("last_deferred") or {}
        if last.get("reason") != reason or last.get("slot_date") != slot_date:
            state["last_deferred"] = {"at": _iso(now), "reason": reason, "slot_date": slot_date}
    _update_state(apply)
    logger.info("skills sleep pass: deferred (%s)", reason)


def _give_back_attempt(state: Dict[str, Any], slot_date: str) -> None:
    attempts = state.get("attempts")
    if isinstance(attempts, dict) and attempts.get("slot_date") == slot_date:
        attempts["n"] = max(0, int(attempts.get("n") or 0) - 1)


async def tick(skills_manager=None, *, deps: Optional[Deps] = None) -> Dict[str, Any]:
    """One look at the clock. Cheap when nothing is due. Returns
    `{"status": ..., "reason": ...}` for logs and tests.

    The decision is taken twice. The first, lock-free, pass keeps an idle tick
    cheap; the one that matters is repeated while holding the cross-process
    state lock, together with the claim of the day and the run lease, so two
    Faustus processes sharing the data directory cannot both start."""
    deps = deps or Deps()
    if deps.skills is None and skills_manager is not None:
        deps.skills = _default_skills(skills_manager)
    now = deps.now()
    enabled, hour = is_enabled(), configured_hour()
    state = load_state()
    if state.get("running_since"):
        _settle_interrupted(now)           # a previous process died mid-pass
        state = load_state()
    decision = evaluate(now, enabled=enabled, hour=hour, last_run_date=state.get("last_run_date"))

    if decision.action == "skip":
        if decision.reason == "outside_window" and state.get("last_missed_date") != decision.slot_date:
            _update_state(lambda s: s.__setitem__("last_missed_date", decision.slot_date))
            logger.info("skills sleep pass: slot %s missed (outside the %dh catch-up window)",
                        decision.slot_date, CATCHUP_HOURS)
        return {"status": "skipped", "reason": decision.reason}

    blocker = deps.blocker()
    if blocker:
        _note_deferred(now, blocker, decision.slot_date)
        return {"status": "deferred", "reason": blocker}

    result: Dict[str, Any]
    with contextlib.ExitStack() as stack:
        # Claim + revalidate under the common lock. Whoever gets here second
        # re-reads the state the first one already wrote and skips.
        busy: Optional[str] = None
        try:
            with _state_lock():
                state = load_state()
                busy = overlap_reason()
                if busy is None:
                    settled = _settle_locked(state, now)
                    decision = evaluate(now, enabled=is_enabled(), hour=configured_hour(),
                                        last_run_date=state.get("last_run_date"))
                    if decision.action == "skip":
                        if settled:
                            save_state(state)
                        return {"status": "skipped", "reason": decision.reason}
                    # The lease is created before the claim is written, so a
                    # visible `running_since` always has an owner behind it.
                    stack.enter_context(track("scheduled", _state_locked=True))
                    attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
                    n = int(attempts.get("n") or 0) if attempts.get("slot_date") == decision.slot_date else 0
                    state["attempts"] = {"slot_date": decision.slot_date, "n": n + 1}
                    state["last_run_date"] = decision.slot_date
                    state["running_since"] = _iso(now)
                    state["running_slot"] = decision.slot_date
                    state.pop("last_deferred", None)
                    save_state(state)
        except SleepPassBusy:
            busy = "sleep_pass_running"
        except LockTimeout:
            busy = "state_lock_timeout"
        if busy:
            _note_deferred(now, busy, decision.slot_date)
            return {"status": "deferred", "reason": busy}

        # From here the lease is held until the stack unwinds, i.e. after the
        # result is written: nobody can mistake a finished run for a dead one.
        try:
            result = await _run_pass(deps, decision.slot_date, now)
        except asyncio.CancelledError:
            # Shutdown in the middle of the pass: give the day back so the next
            # start inside the window can run it (a partial pass left no record).
            def give_back(s: Dict[str, Any]) -> None:
                s.pop("running_since", None)
                s.pop("running_slot", None)
                s["last_run_date"] = _previous_run_date(s, decision.slot_date)
                _give_back_attempt(s, decision.slot_date)
            _update_state(give_back)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("skills sleep pass failed: %s", exc, exc_info=True)
            result = {"trigger": "scheduled", "slot_date": decision.slot_date,
                      "started_at": _iso(now), "status": "failed", "considered": 0,
                      "candidates": 0, "proposals": [],
                      "errors": [{"skill_id": "", "error_class": f"unexpected.{type(exc).__name__}"}],
                      "stopped": "", "retry": True}
        result["finished_at"] = _iso(deps.now())
        retryable = bool(result.pop("retry", False))
        no_work = bool(result.pop("no_work", False))

        def finish(s: Dict[str, Any]) -> None:
            s.pop("running_since", None)
            s.pop("running_slot", None)
            if no_work:
                # Deferred before a single proposal: nothing was done, so the
                # day and the attempt are given back, and it is not a "last run".
                s["last_run_date"] = _previous_run_date(s, decision.slot_date)
                _give_back_attempt(s, decision.slot_date)
                s["last_deferred"] = {"at": result["finished_at"], "reason": result["stopped"],
                                      "slot_date": decision.slot_date}
                return
            s["last_result"] = result
            s["last_run_at"] = result["finished_at"]
            if retryable and int((s.get("attempts") or {}).get("n") or 0) < MAX_ATTEMPTS:
                s["last_run_date"] = _previous_run_date(s, decision.slot_date)
        _update_state(finish)
    if no_work:
        logger.info("skills sleep pass: deferred before any work (%s)", result["stopped"])
        return {"status": "deferred", "reason": result["stopped"]}
    logger.info("skills sleep pass: %s (%d proposal(s), %d error(s))", result["status"],
                len(result["proposals"]), len(result["errors"]))
    return {"status": "ran", "reason": result["status"], "result": result}


def _previous_run_date(state: Mapping[str, Any], slot_date: str) -> Optional[str]:
    """What `last_run_date` was before this slot claimed it: the day before the
    slot, which is anything but the slot - enough for `evaluate` to call the
    slot due again at the next tick."""
    try:
        return (date.fromisoformat(slot_date) - timedelta(days=1)).isoformat()
    except ValueError:
        return None


async def scheduler_loop(skills_manager, *, tick_seconds: float = TICK_SECONDS,
                         startup_delay: float = STARTUP_DELAY_S,
                         deps: Optional[Deps] = None,
                         sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
    """The system loop `app.py` spawns. Survives any error in a tick."""
    await sleep(startup_delay)
    while True:
        try:
            await tick(skills_manager, deps=deps)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("skills sleep pass tick failed", exc_info=True)
        await sleep(tick_seconds)


# -- status for the API -----------------------------------------------------

def status(now: Optional[datetime] = None) -> Dict[str, Any]:
    """What the Proposals tab shows: whether the schedule is on, when it runs
    next, and how the last scheduled run went. Counts only - no skill names -
    because the schedule is machine-wide and the caller may be any user."""
    now = now or datetime.now()
    enabled, hour = is_enabled(), configured_hour()
    state = load_state()
    decision = evaluate(now, enabled=enabled, hour=hour, last_run_date=state.get("last_run_date"))
    running = overlap_reason() is not None          # this process or another one
    # A claimed day whose owner is gone: shown as interrupted until the next
    # tick (or the next claim) writes it down; never repeated.
    if state.get("running_since") and not running:
        # A run that ended between the two reads clears the marker BEFORE it
        # drops the lease, so a second read tells a finished run from a dead one.
        state = load_state()
        running = overlap_reason() is not None
    interrupted = bool(state.get("running_since")) and not running
    if not enabled:
        phase, next_run = "disabled", None
    elif running:
        phase, next_run = "running", None
    elif decision.action == "run":
        phase, next_run = "due", _iso(now)
    else:
        phase, next_run = "scheduled", _iso(next_slot_after(now, hour))
    last = state.get("last_result") if isinstance(state.get("last_result"), dict) else None
    last_view = None
    if last:
        last_view = {
            "status": last.get("status"), "slot_date": last.get("slot_date"),
            "started_at": last.get("started_at"), "finished_at": last.get("finished_at"),
            "considered": last.get("considered", 0), "candidates": last.get("candidates", 0),
            "proposals": len(last.get("proposals") or []),
            "errors": len(last.get("errors") or []), "stopped": last.get("stopped") or "",
        }
    if interrupted:
        last_view = {
            "status": "interrupted", "slot_date": state.get("running_slot") or state.get("last_run_date"),
            "started_at": state.get("running_since"), "finished_at": None,
            "considered": 0, "candidates": 0, "proposals": 0, "errors": 0, "stopped": "",
        }
    deferred = state.get("last_deferred") if isinstance(state.get("last_deferred"), dict) else None
    return {
        "enabled": enabled, "hour": hour, "catchup_hours": CATCHUP_HOURS,
        "phase": phase, "running": running, "interrupted": interrupted, "next_run_at": next_run,
        "last_run_date": state.get("last_run_date"),
        "last_missed_date": state.get("last_missed_date"),
        "waiting_for": (deferred or {}).get("reason") if phase == "due" else None,
        "last_run": last_view,
    }
