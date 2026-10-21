"""The background worker: a bounded queue, one daemon thread, the small model.

The agent loop only ever calls :func:`enqueue` (a lock, a deque append, a
notify). Everything slow happens on the worker's own thread with its own event
loop, so a turn never waits for a model, a database or a lock held by this
module.

Admission follows the repo's rule for unattended model work
(``src/background_job_guard.py``): the call is made only when no chat turn is in
flight, the unattended breaker is closed, the model is already resident (the
worker never makes the runner load one) and the runner has a free slot. "Not
now" puts the job back at the head of the queue and the worker tries again
shortly; a job that stays blocked longer than ``JOB_TTL_S`` is dropped. A full
queue drops its OLDEST job. A model failure, an unusable reply or a storage
error drops that one job with a debug log line — nothing here raises into the
caller.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from . import schema, store

logger = logging.getLogger(__name__)

PURPOSE = "observations"
JOB_TTL_S = 1800.0
POLL_S = 4.0
IDLE_EXIT_S = 120.0
PRUNE_EVERY_S = 6 * 3600.0
OBSERVATION_MAX_TOKENS = 500
SUMMARY_MAX_TOKENS = 450
MODEL_TIMEOUT_S = 45
SUMMARY_NOTES = 40

ModelCall = Callable[..., Awaitable[Any]]


class NotNow(Exception):
    """The model may not be called right now (reason in ``args[0]``)."""


class ModelUnavailable(Exception):
    """There is no model to call."""


# -- settings ---------------------------------------------------------------

def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001 - settings trouble means the default
        return default


def _int_setting(key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(_setting(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def enabled() -> bool:
    try:
        return bool(_setting("agent_observations_enabled", True))
    except Exception:  # noqa: BLE001
        return False


def summaries_enabled() -> bool:
    return enabled() and bool(_setting("agent_observations_summaries", True))


# -- the queue --------------------------------------------------------------

_QUEUE: Deque[Dict[str, Any]] = deque()
_COND = threading.Condition()
_WORKER: Optional[threading.Thread] = None
_STOP = False
_LAST_PRUNE = 0.0
_STATS: Dict[str, Any] = {
    "queued": 0, "dropped_full": 0, "stored": 0, "skipped_by_model": 0, "garbage": 0,
    "errors": 0, "deferred": 0, "expired": 0, "duplicates": 0, "summaries": 0,
    "prefiltered": {}, "last_blocker": "",
}


def count_prefiltered(reason: str) -> None:
    with _COND:
        bucket = _STATS["prefiltered"]
        bucket[reason] = bucket.get(reason, 0) + 1


def _bump(key: str, by: int = 1) -> None:
    with _COND:
        _STATS[key] = _STATS.get(key, 0) + by


def queue_limit() -> int:
    return _int_setting("agent_observations_queue_max", 100, 10, 1000)


def enqueue(job: Dict[str, Any], *, start_worker: bool = True) -> bool:
    """Queue a job. Never blocks, never raises. False when the job was not
    kept (observations off, or an internal error); a full queue keeps this job
    and drops the oldest one instead."""
    try:
        if not enabled():
            return False
        job.setdefault("ts", time.time())
        with _COND:
            _QUEUE.append(job)
            _STATS["queued"] += 1
            limit = queue_limit()
            while len(_QUEUE) > limit:
                _QUEUE.popleft()
                _STATS["dropped_full"] += 1
            _COND.notify()
        if start_worker:
            ensure_worker()
        return True
    except Exception:  # noqa: BLE001 - the agent loop never sees this
        logger.debug("observations: enqueue failed", exc_info=True)
        return False


def pending() -> int:
    with _COND:
        return len(_QUEUE)


def status() -> Dict[str, Any]:
    with _COND:
        stats = dict(_STATS)
        stats["prefiltered"] = dict(_STATS["prefiltered"])
        return {"enabled": enabled(), "pending": len(_QUEUE),
                "worker_alive": bool(_WORKER is not None and _WORKER.is_alive()), "stats": stats}


def reset_for_tests() -> None:
    global _LAST_PRUNE
    with _COND:
        _QUEUE.clear()
        for key, value in list(_STATS.items()):
            _STATS[key] = {} if isinstance(value, dict) else ("" if isinstance(value, str) else 0)
        _LAST_PRUNE = 0.0


def ensure_worker() -> None:
    global _WORKER, _STOP
    with _COND:
        if _WORKER is not None and _WORKER.is_alive():
            return
        _STOP = False
        _WORKER = threading.Thread(target=_worker_loop, name="observations", daemon=True)
        _WORKER.start()


def shutdown(timeout: float = 3.0) -> None:
    """Ask the worker thread to leave after its current job and wait for it.
    The next ``enqueue`` starts a new one."""
    global _STOP
    with _COND:
        _STOP = True
        thread = _WORKER
        _COND.notify_all()
    if thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout)


def _worker_loop() -> None:
    global _WORKER
    idle_since = time.monotonic()
    while True:
        with _COND:
            while not _QUEUE:
                if _STOP or time.monotonic() - idle_since > IDLE_EXIT_S:
                    _WORKER = None
                    return
                _COND.wait(timeout=5.0)
            if _STOP:
                _WORKER = None
                return
            job = _QUEUE.popleft()
        idle_since = time.monotonic()
        try:
            from src import owned_loop
            outcome = owned_loop.run(process_job(job))
        except Exception:  # noqa: BLE001
            logger.debug("observations: job crashed", exc_info=True)
            _bump("errors")
            outcome = "error"
        if outcome.startswith("deferred"):
            with _COND:
                if len(_QUEUE) < queue_limit():
                    _QUEUE.appendleft(job)
            time.sleep(POLL_S)
        _maybe_prune()


def _maybe_prune() -> None:
    global _LAST_PRUNE
    now = time.time()
    if now - _LAST_PRUNE < PRUNE_EVERY_S:
        return
    _LAST_PRUNE = now
    try:
        days = _int_setting("agent_observations_retention_days", 90, 0, 3650)
        if days:
            store.prune(days)
    except Exception:  # noqa: BLE001
        logger.debug("observations: prune failed", exc_info=True)


# -- admission and the model --------------------------------------------------

def admission_blocker(url: str, model: str) -> Optional[str]:
    """None when an unattended call to `model` may start now, else a short
    stable reason. A probe that cannot answer never blocks, except the
    residency check, which answers "would load" when it cannot tell."""
    try:
        from src import unattended_breaker
        if unattended_breaker.is_open():
            return "unattended_breaker_open"
    except Exception:  # noqa: BLE001
        logger.debug("observations: breaker probe failed", exc_info=True)
    try:
        from src.context_engine import maintenance
        if maintenance.should_yield():
            return "interactive_turn"
    except Exception:  # noqa: BLE001
        logger.debug("observations: interactive probe failed", exc_info=True)
    try:
        from src import background_job_guard as guard
        if not guard.should_run_with_model(PURPOSE, url, model, user_initiated=False):
            return "would_load_model"
        if guard.model_busy(url) or guard.no_free_slot(url):
            return "model_busy"
    except Exception:  # noqa: BLE001
        logger.debug("observations: model guard failed", exc_info=True)
        return "model_guard_error"
    return None


async def default_model_call(messages: List[Dict[str, str]], *, owner: str = "", max_tokens: int = OBSERVATION_MAX_TOKENS) -> str:
    """One call to the small model: the utility endpoint (or the model named in
    ``agent_observations_model`` on it), reasoning off, a short answer.
    Raises :class:`NotNow` or :class:`ModelUnavailable`; any other exception
    is the call failing."""
    from src.endpoint_resolver import resolve_endpoint
    url, model, headers = resolve_endpoint("utility", owner=owner or None)
    override = str(_setting("agent_observations_model", "") or "").strip()
    if override:
        model = override
    if not url or not model:
        raise ModelUnavailable("no_endpoint")
    blocker = admission_blocker(url, model)
    if blocker:
        raise NotNow(blocker)
    from src.llm_core import llm_call_async
    raw = await llm_call_async(
        url=url, model=model, messages=messages, headers=headers, temperature=0.1,
        max_tokens=max_tokens, timeout=MODEL_TIMEOUT_S, max_retries=1, workload="background",
        gen_overrides={"think": False},
    )
    return raw[0] if isinstance(raw, tuple) else raw


# -- prompts ------------------------------------------------------------------

OBSERVATION_PROMPT = """You write one memory note about ONE tool call that an AI coding agent just made, so a later session can find what happened without redoing the work.

Reply with ONE JSON object and nothing else.

If the call taught nothing (a listing, a read with no finding, a failed retry, a status check), reply exactly:
{"skip": true, "reason": "noise"}

Otherwise reply with:
{"type": "bugfix|feature|refactor|change|discovery|decision|other",
 "title": "short noun phrase, at most 12 words",
 "subtitle": "one sentence on the outcome",
 "facts": ["a statement that stays true on its own: name the file, function or value; never use it/this/they/the file", "..."],
 "narrative": "two sentences at most: what was done and why",
 "concepts": ["how-it-works|why-it-exists|gotcha|pattern|trade-off|problem-solution"],
 "files_read": ["path"],
 "files_modified": ["path"]}

Rules: use only what the call below shows, never guess; keep every field short; write in the language of the user request; never copy secrets, tokens or passwords.

Tool: {tool}
User request of this turn: {request}
Arguments: {args}
Result{failed}:
{result}
"""

SUMMARY_PROMPT = """Summarise the work of one agent turn from the notes it left. Reply with ONE JSON object and nothing else:
{"request": "what the user asked", "investigated": "what was looked at", "learned": "what was found out", "completed": "what was done", "next_steps": "what remains"}
Each value is one or two short sentences, and "" when there is nothing. Write in the language of the user request. If the notes show nothing worth keeping, reply exactly {"skip": true, "reason": "empty"}.

User request: {request}
Notes, oldest first:
{notes}
"""


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_observation_messages(job: Dict[str, Any]) -> List[Dict[str, str]]:
    prompt = OBSERVATION_PROMPT
    for key, value in (
        ("{tool}", _clip(job.get("tool"), 80)),
        ("{request}", _clip(job.get("request"), 300) or "(unknown)"),
        ("{args}", job.get("args") or ""),
        ("{failed}", " (the call failed)" if job.get("failed") else ""),
        ("{result}", job.get("result") or "(empty)"),
    ):
        prompt = prompt.replace(key, str(value))
    return [{"role": "user", "content": prompt}]


def build_summary_messages(request: str, notes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    lines = []
    for note in notes[-SUMMARY_NOTES:]:
        line = f"- [{note.get('type')}] {note.get('title')}"
        if note.get("subtitle"):
            line += f" - {note['subtitle']}"
        facts = "; ".join(_clip(f, 140) for f in (note.get("facts") or [])[:3])
        if facts:
            line += f"\n  {facts}"
        lines.append(line)
    prompt = SUMMARY_PROMPT.replace("{request}", _clip(request, 400) or "(unknown)").replace("{notes}", "\n".join(lines))
    return [{"role": "user", "content": prompt}]


# -- processing one job ---------------------------------------------------------

async def process_job(job: Dict[str, Any], *, model_call: Optional[ModelCall] = None,
                      now: Optional[float] = None) -> str:
    """Run one queued job. Returns a short outcome word: ``stored``,
    ``skipped``, ``garbage``, ``duplicate``, ``disabled``, ``expired``,
    ``no_model``, ``error`` or ``deferred:<reason>`` (the caller retries).
    Never raises."""
    try:
        if not enabled():
            return "disabled"
        stamp = time.time() if now is None else now
        if stamp - float(job.get("ts") or stamp) > JOB_TTL_S:
            _bump("expired")
            return "expired"
        call = model_call or default_model_call
        if job.get("kind") == "summary":
            return await _process_summary(job, call)
        return await _process_observation(job, call)
    except NotNow as exc:
        reason = str(exc.args[0]) if exc.args else "not_now"
        with _COND:
            _STATS["deferred"] += 1
            _STATS["last_blocker"] = reason
        return f"deferred:{reason}"
    except ModelUnavailable:
        _bump("errors")
        return "no_model"
    except Exception:  # noqa: BLE001 - one job never takes the worker down
        logger.debug("observations: job failed", exc_info=True)
        _bump("errors")
        return "error"


async def _process_observation(job: Dict[str, Any], call: ModelCall) -> str:
    raw = await call(build_observation_messages(job), owner=job.get("owner", ""), max_tokens=OBSERVATION_MAX_TOKENS)
    parsed = schema.parse_observation_reply(raw)
    if parsed is None:
        _bump("garbage")
        return "garbage"
    if isinstance(parsed, schema.Skip):
        _bump("skipped_by_model")
        return "skipped"
    for path in job.get("files_read") or []:
        if path not in parsed.files_read:
            parsed.files_read.append(path)
    for path in job.get("files_modified") or []:
        if path not in parsed.files_modified:
            parsed.files_modified.append(path)
    obs_id = store.add_observation(
        parsed, owner=job.get("owner", ""), session_id=job.get("session_id", ""), turn_id=job.get("turn_id", ""),
        tool=job.get("tool", ""), project=job.get("project", ""), workspace=job.get("workspace", ""),
        created=job.get("ts"))
    if obs_id is None:
        _bump("duplicates")
        return "duplicate"
    _bump("stored")
    return "stored"


async def _process_summary(job: Dict[str, Any], call: ModelCall) -> str:
    if not summaries_enabled():
        return "disabled"
    session_id, turn_id = job.get("session_id", ""), job.get("turn_id", "")
    notes = store.turn_observations(session_id, turn_id)
    if not notes:
        return "skipped"
    request = job.get("request") or notes[0].get("title", "")
    raw = await call(build_summary_messages(request, notes), owner=job.get("owner", "") or notes[0].get("owner", ""),
                     max_tokens=SUMMARY_MAX_TOKENS)
    parsed = schema.parse_summary_reply(raw)
    if parsed is None:
        _bump("garbage")
        return "garbage"
    if isinstance(parsed, schema.Skip):
        _bump("skipped_by_model")
        return "skipped"
    first = notes[0]
    store.add_summary(
        parsed, owner=job.get("owner", ""), session_id=session_id, turn_id=turn_id,
        project=first.get("project", ""), workspace=first.get("workspace", ""),
        from_obs_id=notes[0]["id"], to_obs_id=notes[-1]["id"], obs_count=len(notes))
    _bump("summaries")
    return "stored"
