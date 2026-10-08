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
      previous scheduled one) - `track()` is the shared registry;
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
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from core.atomic_io import atomic_write_json

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

_STATE_LOCK = threading.RLock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Dict[int, Dict[str, Any]] = {}
_ACTIVE_SEQ = 0


# -- shared "a pass is running" registry ------------------------------------

@contextlib.contextmanager
def track(label: str = "manual") -> Iterator[None]:
    """Marks a sleep pass as in progress for the duration of the block. The
    on-demand route and the scheduled run both wrap their work in it, which is
    what lets the scheduler refuse to overlap either of them."""
    global _ACTIVE_SEQ
    with _ACTIVE_LOCK:
        _ACTIVE_SEQ += 1
        token = _ACTIVE_SEQ
        _ACTIVE[token] = {"label": label, "since": time.time()}
    try:
        yield
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(token, None)


def active_runs() -> List[Dict[str, Any]]:
    with _ACTIVE_LOCK:
        return [dict(v) for v in _ACTIVE.values()]


def reset_for_tests() -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.clear()


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


def admission_blocker(*, url: Optional[str] = None, model: Optional[str] = None) -> Optional[str]:
    """None when the pass may start; otherwise a short stable reason. Every
    probe is the existing one the other unattended jobs use, and each is
    wrapped: a probe that itself fails never blocks forever (the model-load
    guard, which is the one that matters for VRAM, fails closed on its own)."""
    if active_runs():
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
        if index:
            blocker = deps.blocker(url=url, model=model)
            if blocker:
                result["stopped"] = blocker
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
    return result


async def tick(skills_manager=None, *, deps: Optional[Deps] = None) -> Dict[str, Any]:
    """One look at the clock. Cheap when nothing is due. Returns
    `{"status": ..., "reason": ...}` for logs and tests."""
    deps = deps or Deps()
    if deps.skills is None and skills_manager is not None:
        deps.skills = _default_skills(skills_manager)
    now = deps.now()
    enabled, hour = is_enabled(), configured_hour()
    with _STATE_LOCK:
        state = load_state()
    decision = evaluate(now, enabled=enabled, hour=hour, last_run_date=state.get("last_run_date"))

    if decision.action == "skip":
        if decision.reason == "outside_window" and state.get("last_missed_date") != decision.slot_date:
            with _STATE_LOCK:
                state = load_state()
                state["last_missed_date"] = decision.slot_date
                save_state(state)
            logger.info("skills sleep pass: slot %s missed (outside the %dh catch-up window)",
                        decision.slot_date, CATCHUP_HOURS)
        return {"status": "skipped", "reason": decision.reason}

    blocker = deps.blocker()
    if blocker:
        with _STATE_LOCK:
            state = load_state()
            last = state.get("last_deferred") or {}
            if last.get("reason") != blocker or last.get("slot_date") != decision.slot_date:
                state["last_deferred"] = {"at": _iso(now), "reason": blocker,
                                          "slot_date": decision.slot_date}
                save_state(state)
        logger.info("skills sleep pass: deferred (%s)", blocker)
        return {"status": "deferred", "reason": blocker}

    # Claim the day BEFORE any work (restart-safe), release it if the work
    # turns out to be a retryable nothing.
    with _STATE_LOCK:
        state = load_state()
        attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
        n = int(attempts.get("n") or 0) if attempts.get("slot_date") == decision.slot_date else 0
        state["attempts"] = {"slot_date": decision.slot_date, "n": n + 1}
        state["last_run_date"] = decision.slot_date
        state["running_since"] = _iso(now)
        state.pop("last_deferred", None)
        save_state(state)

    result: Dict[str, Any]
    try:
        with track("scheduled"):
            result = await _run_pass(deps, decision.slot_date, now)
    except asyncio.CancelledError:
        # Shutdown in the middle of the pass: give the day back so the next
        # start inside the window can run it (a partial pass left no record).
        with _STATE_LOCK:
            state = load_state()
            state.pop("running_since", None)
            state["last_run_date"] = _previous_run_date(state, decision.slot_date)
            save_state(state)
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
    with _STATE_LOCK:
        state = load_state()
        state.pop("running_since", None)
        state["last_result"] = result
        state["last_run_at"] = result["finished_at"]
        if retryable and int((state.get("attempts") or {}).get("n") or 0) < MAX_ATTEMPTS:
            state["last_run_date"] = _previous_run_date(state, decision.slot_date)
        save_state(state)
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
    running = bool(active_runs())
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
    deferred = state.get("last_deferred") if isinstance(state.get("last_deferred"), dict) else None
    return {
        "enabled": enabled, "hour": hour, "catchup_hours": CATCHUP_HOURS,
        "phase": phase, "running": running, "next_run_at": next_run,
        "last_run_date": state.get("last_run_date"),
        "last_missed_date": state.get("last_missed_date"),
        "waiting_for": (deferred or {}).get("reason") if phase == "due" else None,
        "last_run": last_view,
    }
