# src/llm_core.py
import httpx
import asyncio
import copy
import time
import json
import logging
import hashlib
import threading
import re
import os
import math
import unicodedata
import weakref
from contextlib import asynccontextmanager
from fastapi import HTTPException
from typing import Any, Optional, Dict, List, Tuple, Callable, Mapping
from src.model_context import get_context_length, DEFAULT_CONTEXT, is_local_endpoint
from src.tool_call_assembler import ToolCallAssembler
from src.retry_policy import (
    RetryClass, RetryBudget, classify_http, parse_retry_after,
    delay as _retry_delay, error_class_for as _retry_error_class,
)
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCAL_MODEL_LOCK = asyncio.Lock()
_LOCAL_MODEL_WAITING_FOREGROUND = 0
_LOCAL_MODEL_CURRENT: Dict[str, object] = {}
#: Foreground calls sharing the slot holder's server (see
#: `_can_share_local_slot`): {"host", "model", "count"}.
_LOCAL_MODEL_SHARED: Dict[str, object] = {"host": "", "model": "", "count": 0}


def _normalize_usage_counts(input_value=0, output_value=0):
    """Return safe integer token counts, or ``None`` for malformed usage."""

    def _count(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if isinstance(value, int):
            count = value
        else:
            if not math.isfinite(value) or not value.is_integer():
                return None
            count = int(value)
        if count < 0 or count > (2**63 - 1):
            return None
        return count

    input_tokens = _count(input_value)
    output_tokens = _count(output_value)
    if input_tokens is None or output_tokens is None:
        return None
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _extract_usage_extras(usage) -> dict:
    """Pull OpenRouter's REAL-cost fields out of a raw provider ``usage``
    payload, when they are there.

    OBJ-8/A1: OpenRouter only returns ``usage.cost`` (actual USD spent),
    ``usage.cost_details.upstream_inference_cost``,
    ``usage.prompt_tokens_details.cached_tokens`` and
    ``usage.completion_tokens_details.reasoning_tokens`` when the request
    opted in with ``payload["usage"] = {"include": True}``
    (`src/openrouter_options.py`, Lote A2 — this module never sets that
    itself). Every other provider's ``usage`` dict simply lacks these keys,
    so this returns ``{}`` for them — ABSENT keys, never ``None`` ones, so a
    caller can blindly ``dict.update()`` the result into a usage dict without
    inventing a field that was never reported. A malformed/non-numeric value
    (NaN, a string, a negative cost) is treated the same as absent rather
    than propagated.
    """
    if not isinstance(usage, dict):
        return {}

    def _finite_nonneg(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return None
        return value

    extras = {}

    cost = _finite_nonneg(usage.get("cost"))
    if cost is not None:
        extras["cost_usd"] = cost
        extras["cost_source"] = "provider"

    cost_details = usage.get("cost_details")
    if isinstance(cost_details, dict):
        upstream = _finite_nonneg(cost_details.get("upstream_inference_cost"))
        if upstream is not None:
            extras["upstream_cost_usd"] = upstream

    prompt_details = usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        cached = _finite_nonneg(prompt_details.get("cached_tokens"))
        if cached is not None:
            extras["cached_tokens"] = int(cached)

    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning = _finite_nonneg(completion_details.get("reasoning_tokens"))
        if reasoning is not None:
            extras["reasoning_tokens"] = int(reasoning)

    return extras


def _ollama_rate(count, duration_ns) -> Optional[float]:
    """Tokens per second from Ollama's own counters, or None.

    Ollama reports `eval_count` / `eval_duration` (nanoseconds) on the final
    chunk of a native `/api/chat` stream: the same pure-decode speed llama.cpp
    calls `predicted_per_second`. A malformed or zero pair yields nothing
    rather than a made-up number — the caller then falls back to wall-clock
    and says so (`tps_source`).
    """
    if isinstance(count, bool) or isinstance(duration_ns, bool):
        return None
    if not isinstance(count, (int, float)) or not isinstance(duration_ns, (int, float)):
        return None
    if not math.isfinite(count) or not math.isfinite(duration_ns):
        return None
    if count <= 0 or duration_ns <= 0:
        return None
    return round(count / (duration_ns / 1_000_000_000), 2)


def _finite_number(value: Any) -> Optional[float]:
    """A plain finite number, or `None` — never coerces a bool, string or
    NaN/inf into a metric. Shared by the ms/count readers below so a
    malformed engine field becomes absent instead of a wrong number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _ms_from_ns(value: Any) -> Optional[float]:
    """Nanoseconds to milliseconds, or `None` for a missing/non-numeric
    field. Ollama's `done` message reports load/prompt/eval durations in ns
    (INF-03 §08); a field Ollama did not send must stay `None` (absent),
    never become 0."""
    value = _finite_number(value)
    return round(value / 1_000_000.0, 3) if value is not None else None


def _finite_ms(value: Any) -> Optional[float]:
    """Same numeric guard as `_ms_from_ns`, without the ns→ms conversion —
    llama.cpp's `timings` block already reports milliseconds."""
    value = _finite_number(value)
    return round(value, 3) if value is not None else None


def _safe_count(value: Any) -> Optional[int]:
    """An integral token/eval count, or `None` — a float that is not a
    whole number (or any non-numeric/bool value) is treated as absent
    rather than truncated into a wrong count."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
        return None
    return int(value)


def _ollama_engine_timings(j: dict) -> Dict[str, Any]:
    """INF-03: Ollama's own per-request phase durations, converted from the
    nanoseconds `done` reports to milliseconds. `load_duration` and
    `total_duration` were already on `j` but thrown away — only
    `eval_count`/`eval_duration`/`prompt_eval_count`/`prompt_eval_duration`
    were read (for `_ollama_rate` above). This keeps the raw phase
    breakdown so a client can show *why* a turn took as long as it did, not
    just its overall throughput. A field Ollama omits stays `None` (absent),
    never 0 — `_ms_from_ns`/`_safe_count` enforce that."""
    return {
        "load_ms": _ms_from_ns(j.get("load_duration")),
        "prompt_ms": _ms_from_ns(j.get("prompt_eval_duration")),
        "predicted_ms": _ms_from_ns(j.get("eval_duration")),
        "total_ms": _ms_from_ns(j.get("total_duration")),
        "prompt_n": _safe_count(j.get("prompt_eval_count")),
        "predicted_n": _safe_count(j.get("eval_count")),
        "source": "ollama",
    }


def _llamacpp_engine_timings(tm: dict) -> Dict[str, Any]:
    """INF-03: llama.cpp's `timings` block, kept in the same shape as
    `_ollama_engine_timings` so a client reads one field name regardless of
    backend. llama.cpp does not report load time per request — `load_ms`
    stays `None` here always, never guessed from some other figure."""
    return {
        "load_ms": None,
        "prompt_ms": _finite_ms(tm.get("prompt_ms")),
        "predicted_ms": _finite_ms(tm.get("predicted_ms")),
        "prompt_n": _safe_count(tm.get("prompt_n")),
        "predicted_n": _safe_count(tm.get("predicted_n")),
        "source": "llamacpp",
    }


# What each local model really decodes at, learned from Ollama's own
# counters on every reply. Deep Research sizes the wait for a non-streamed
# call with it (deep_research._call_budget): a guessed 8 tok/s is right for
# a 27B q4 on consumer cards and wrong for everything else.
_LOCAL_SPEED: Dict[str, Dict[str, float]] = {}
_LOCAL_SPEED_LOCK = threading.Lock()
_LOCAL_SPEED_LOADED_FROM_DISK = False
_LOCAL_SPEED_LAST_PERSIST = 0.0
_LOCAL_SPEED_PERSIST_MIN_INTERVAL_S = 5.0


def _local_speed_path() -> str:
    try:
        from src.constants import DATA_DIR
    except Exception:  # pragma: no cover - mirrors src/token_calibration.py's fallback
        DATA_DIR = os.path.join(os.getcwd(), "data")
    return os.path.join(DATA_DIR, "local_speed.json")


def _load_local_speed_from_disk() -> None:
    """Hydrate ``_LOCAL_SPEED`` once per process from the figures the last
    run persisted (CMP-08 follow-up), so ``local_speed()`` for a model this
    process has not yet replied from — a fresh restart, no GPU speed
    measured yet THIS run — still surfaces the last real decode speed
    Faustus observed, instead of ``unknown`` until it happens to reply
    again. A model this run has already measured is never overwritten by
    the stale on-disk figure."""
    global _LOCAL_SPEED_LOADED_FROM_DISK
    if _LOCAL_SPEED_LOADED_FROM_DISK:
        return
    with _LOCAL_SPEED_LOCK:
        if _LOCAL_SPEED_LOADED_FROM_DISK:
            return
        _LOCAL_SPEED_LOADED_FROM_DISK = True
        try:
            path = _local_speed_path()
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                return
            for model, entry in raw.items():
                if (
                    isinstance(model, str) and model and model not in _LOCAL_SPEED
                    and isinstance(entry, dict) and isinstance(entry.get("tps"), (int, float))
                ):
                    _LOCAL_SPEED[model] = {
                        "tps": float(entry["tps"]),
                        "samples": float(entry.get("samples") or 0.0),
                    }
        except Exception:
            logger.debug("[llm_core] local_speed disk cache unreadable", exc_info=True)


_LOCAL_SPEED_PERSIST_THREAD: Optional[threading.Thread] = None


def _persist_local_speed() -> None:
    """Best-effort, throttled, off-thread — never adds real latency to the
    reply this measurement came from and never raises."""
    global _LOCAL_SPEED_LAST_PERSIST, _LOCAL_SPEED_PERSIST_THREAD
    now = time.time()
    if now - _LOCAL_SPEED_LAST_PERSIST < _LOCAL_SPEED_PERSIST_MIN_INTERVAL_S:
        return
    _LOCAL_SPEED_LAST_PERSIST = now
    snapshot = {
        model: {"tps": data.get("tps"), "samples": data.get("samples")}
        for model, data in dict(_LOCAL_SPEED).items()
        if isinstance(data, dict) and isinstance(data.get("tps"), (int, float))
    }

    def _write() -> None:
        try:
            path = _local_speed_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + f".{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f)
            os.replace(tmp, path)
        except Exception:
            logger.debug("[llm_core] local_speed persist failed", exc_info=True)

    thread = threading.Thread(target=_write, daemon=True, name="local-speed-persist")
    _LOCAL_SPEED_PERSIST_THREAD = thread
    thread.start()


def flush_local_speed_for_tests(timeout: float = 5.0) -> None:
    """Block until the latest background persist finishes writing (or the
    timeout elapses). Test-only — production code never waits on this."""
    thread = _LOCAL_SPEED_PERSIST_THREAD
    if thread is not None:
        thread.join(timeout=timeout)


def remember_local_speed(model: str, count, duration_ns) -> Optional[float]:
    """Fold one reply's decode speed into the model's running figure."""
    tps = _ollama_rate(count, duration_ns)
    if tps is None or not model:
        return None
    if count < 16:                       # a 3-token "cinco" measures nothing
        return None
    cur = _LOCAL_SPEED.get(model)
    if cur is None:
        _LOCAL_SPEED[model] = {"tps": tps, "samples": 1.0}
    else:
        # A slow-moving average: one spilling call must not halve the budget
        # for the rest of the session, one fast one must not shrink it.
        cur["tps"] = round(cur["tps"] * 0.7 + tps * 0.3, 2)
        cur["samples"] += 1
    _persist_local_speed()
    return _LOCAL_SPEED[model]["tps"]


def local_speed(model: str) -> Optional[float]:
    """The learned decode speed of a local model in tok/s, or None.

    Checks this process's own live measurements first; if this model has not
    replied yet this run, falls back to the last figure persisted to disk
    (CMP-08 follow-up) before giving up and returning ``None`` ("unknown")."""
    key = str(model or "")
    cur = _LOCAL_SPEED.get(key)
    if cur:
        return cur["tps"]
    if not key:
        return None
    _load_local_speed_from_disk()
    cur = _LOCAL_SPEED.get(key)
    return cur["tps"] if cur else None


def _normalize_http_status(value) -> Optional[int]:
    """Accept only genuine three-digit integral HTTP status values."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        status = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        status = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"\d{3}", text):
            return None
        status = int(text)
    else:
        return None
    return status if 100 <= status <= 599 else None


def _local_model_gate_enabled() -> bool:
    return os.getenv("ODYSSEUS_LOCAL_MODEL_GATE", "true").lower() not in {"0", "false", "no", "off"}


def _gate_workload(workload: Optional[str]) -> str:
    return "background" if str(workload or "").lower() == "background" else "foreground"


#: How long a request waits on the local model slot before each diagnostic
#: line naming who holds it.
_LOCAL_MODEL_WAIT_LOG_SECONDS = 60.0


async def _acquire_local_model_lock(model: str) -> None:
    """Acquire the local model slot, saying who holds it while waiting.

    Live, 24-09-2026 (exam run 18): a recovery step after a reasoning-loop
    abort waited on the slot with no log line at all, the model server
    idle, for over a quarter of an hour. Every minute of waiting now logs
    the holder (task, workload, model, age); a holder task that has already
    finished can never release the slot — its stream generator was left
    suspended — so the slot is reclaimed instead of waiting forever."""
    waited = 0.0
    while True:
        try:
            await asyncio.wait_for(_LOCAL_MODEL_LOCK.acquire(), timeout=_LOCAL_MODEL_WAIT_LOG_SECONDS)
            return
        except asyncio.TimeoutError:
            waited += _LOCAL_MODEL_WAIT_LOG_SECONDS
            holder = dict(_LOCAL_MODEL_CURRENT)
            owner = holder.get("owner")
            age = time.time() - float(holder.get("started") or time.time())
            logger.warning(
                "[model-gate] model=%s waited %.0fs for the local model slot; held by owner=%s "
                "(finished task=%s) workload=%s model=%s for %.0fs",
                model, waited,
                owner.get_name() if isinstance(owner, asyncio.Task) else type(owner).__name__,
                owner.done() if isinstance(owner, asyncio.Task) else None,
                holder.get("workload"), holder.get("model"), age,
            )
            # Only a holder that IS a task, and has finished, is orphaned; a
            # run-level owner (a turn advancing step by step) is alive even
            # when the task that took the slot has ended.
            if isinstance(owner, asyncio.Task) and owner.done() and _LOCAL_MODEL_LOCK.locked():
                logger.warning("[model-gate] the holder task has finished; reclaiming the orphaned slot")
                _LOCAL_MODEL_CURRENT.clear()
                _LOCAL_MODEL_LOCK.release()


def _shared_slot_enabled() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("local_model_shared_slots", True))
    except Exception:  # noqa: BLE001
        return True


def _host_key(url: str) -> str:
    try:
        from src.swarm.lane import host_key
        return host_key(url)
    except Exception:  # noqa: BLE001
        return ""


async def _server_slots(url: str) -> int:
    """Parallel slots of the llama-server behind `url` (cached for a minute
    by `src.swarm.capacity`), 1 when unknown or not a llama-server."""
    try:
        from src.swarm import capacity
        if capacity._looks_ollama(url):  # its parallelism cannot be read back
            return 1
        slots = await capacity.llamacpp_slots(url, timeout=1.5)
        return int(slots or 1)
    except Exception:  # noqa: BLE001
        return 1


async def _can_share_local_slot(target_url: str, model: str) -> bool:
    """Whether a foreground call may run beside the one holding the slot.

    The slot is one lock for the whole instance because most local servers
    have one generation pipe and a second model must never be loaded while
    one is answering. Two chats of the same instance asking the same
    llama-server, already serving that model with several slots (``-np 4``),
    waited for each other anyway ("waited 60s for the local model slot",
    25-09). Such a call joins the holder instead: same server, same model,
    both in the foreground, and fewer calls in flight than the server has
    slots. Nothing new is loaded, so the VRAM the slot protects is not
    touched; a call to any other server or model still waits."""
    if not (_shared_slot_enabled() and _LOCAL_MODEL_LOCK.locked()):
        return False
    holder = dict(_LOCAL_MODEL_CURRENT)
    host = _host_key(target_url)
    if (not host or holder.get("workload") != "foreground"
            or _host_key(str(holder.get("url") or "")) != host
            or str(holder.get("model") or "") != str(model or "")):
        return False
    slots = await _server_slots(target_url)
    in_flight = 1 + int(_LOCAL_MODEL_SHARED.get("count") or 0)
    return slots > 1 and in_flight < slots


async def _wait_for_other_shared_calls(target_url: str, model: str, kind: str) -> None:
    """After taking the slot: calls that joined the previous holder on
    another server or model must finish before this one starts (it may load
    a model). Same server and model in the foreground: nothing to wait for."""
    while int(_LOCAL_MODEL_SHARED.get("count") or 0) > 0:
        same = (kind == "foreground"
                and _LOCAL_MODEL_SHARED.get("host") == _host_key(target_url)
                and _LOCAL_MODEL_SHARED.get("model") == str(model or ""))
        if same:
            return
        await asyncio.sleep(0.1)


def _foreground_model_busy() -> bool:
    """A foreground call is waiting for the local model slot or holds it."""
    if _LOCAL_MODEL_WAITING_FOREGROUND > 0:
        return True
    if int(_LOCAL_MODEL_SHARED.get("count") or 0) > 0:
        return True
    return _LOCAL_MODEL_LOCK.locked() and _LOCAL_MODEL_CURRENT.get("workload") == "foreground"


@asynccontextmanager
async def _local_model_slot(target_url: str, model: str, workload: Optional[str] = None):
    """Serialize local model traffic, with foreground chat taking priority.

    Most local servers expose one GPU/CPU generation pipe even when their HTTP
    API accepts multiple requests. Letting scheduled email/tasks and foreground
    chat hit that pipe together creates the user-visible "streams crossed" and
    "prompt waited behind a task" failure mode. Cloud providers are left alone.
    """
    if not is_local_endpoint(target_url):
        yield
        return

    # Engine swap (src/engine_swap.py): start a managed llama.cpp engine on
    # demand if `target_url` maps to one and it is not already up, and track
    # usage (in-flight + last-used) for the idle reaper. Guarded throughout:
    # any problem here must never block or break the chat call.
    engine_swap = None
    try:
        from src import engine_swap as _engine_swap
        engine_swap = _engine_swap
        await engine_swap.ensure_ready(target_url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[engine-swap] ensure_ready skipped: %s", exc)

    engine_id = None
    try:
        if engine_swap is not None:
            engine_id = engine_swap.begin(target_url)
    except Exception:  # noqa: BLE001
        engine_id = None

    try:
        if not _local_model_gate_enabled():
            yield
            return

        # Swarm lane (src/swarm/lane.py): a swarm item calling the server
        # it was sized for skips the one-pipe lock -- its runner already
        # caps its calls at the server's parallel slots -- after yielding to
        # any foreground call that is waiting for or holding the model.
        try:
            from src.swarm.lane import lane_for as _swarm_lane_for
            _swarm_lane = _swarm_lane_for(target_url)
        except Exception:  # noqa: BLE001
            _swarm_lane = None
        if _swarm_lane is not None:
            while _foreground_model_busy():
                await asyncio.sleep(0.25)
            yield
            return

        global _LOCAL_MODEL_WAITING_FOREGROUND
        kind = _gate_workload(workload)
        current_task = asyncio.current_task()
        from src.model_slot_owner import current_owner as _current_slot_owner
        current_owner = _current_slot_owner()
        # Re-entrant for the run that already holds the slot. The agent
        # loop handles a stream's error event while that stream's generator
        # is still suspended inside this context manager, lock held; its
        # recovery ladder then asks the local server again from the same
        # run and waited on the lock forever (seen live: a reasoning loop
        # abort, then "[recovery] step=3" and thirteen silent minutes). The
        # owner is the run (src/model_slot_owner.py), not the task: the agent
        # loop advances in a new task per step.
        if (
            current_owner is not None
            and _LOCAL_MODEL_LOCK.locked()
            and _LOCAL_MODEL_CURRENT.get("owner") is current_owner
        ):
            logger.info("[model-gate] this run already holds the local model slot; "
                        "nested call proceeds model=%s", model)
            yield
            return
        if kind == "foreground" and await _can_share_local_slot(target_url, model):
            _LOCAL_MODEL_SHARED.update({"host": _host_key(target_url), "model": str(model or ""),
                                        "count": int(_LOCAL_MODEL_SHARED.get("count") or 0) + 1})
            logger.info("[model-gate] sharing the local server's slots with the holder model=%s (%d beside it)",
                        model, _LOCAL_MODEL_SHARED["count"])
            try:
                yield
            finally:
                _LOCAL_MODEL_SHARED["count"] = max(0, int(_LOCAL_MODEL_SHARED.get("count") or 0) - 1)
            return
        if kind == "foreground":
            _LOCAL_MODEL_WAITING_FOREGROUND += 1
            current = dict(_LOCAL_MODEL_CURRENT)
            if current.get("workload") == "background":
                task = current.get("task")
                if isinstance(task, asyncio.Task) and not task.done():
                    logger.info(
                        "[model-gate] cancelling background local model call for foreground request model=%s",
                        model,
                    )
                    task.cancel()
        else:
            # Background work should not jump in while the browser/chat is active
            # or while a foreground request is waiting to acquire the local model.
            try:
                from src.interactive_gate import has_foreground_activity
            except Exception:
                has_foreground_activity = lambda: False  # type: ignore
            while _LOCAL_MODEL_WAITING_FOREGROUND > 0 or has_foreground_activity():
                await asyncio.sleep(0.25)

        acquired = False
        try:
            await _acquire_local_model_lock(model)
            acquired = True
            await _wait_for_other_shared_calls(target_url, model, kind)
            if kind == "foreground":
                _LOCAL_MODEL_WAITING_FOREGROUND = max(0, _LOCAL_MODEL_WAITING_FOREGROUND - 1)
            _LOCAL_MODEL_CURRENT.clear()
            _LOCAL_MODEL_CURRENT.update({
                "task": current_task,
                "owner": current_owner,
                "workload": kind,
                "url": target_url,
                "model": model,
                "started": time.time(),
            })
            yield
        finally:
            if kind == "foreground":
                _LOCAL_MODEL_WAITING_FOREGROUND = max(0, _LOCAL_MODEL_WAITING_FOREGROUND - 1)
            if acquired and _LOCAL_MODEL_LOCK.locked():
                owner = _LOCAL_MODEL_CURRENT.get("owner")
                if owner is current_owner:
                    _LOCAL_MODEL_CURRENT.clear()
                _LOCAL_MODEL_LOCK.release()
    finally:
        try:
            if engine_swap is not None:
                engine_swap.end(engine_id)
                engine_swap.touch(target_url)
        except Exception:  # noqa: BLE001
            pass

class LLMConfig:
    """Configuration constants for LLM operations."""
    DEFAULT_TIMEOUT = 30
    #: The same default is far too tight for a quantised local model that
    #: reasons before it answers: measured on this machine's 27B, a
    #: three-sentence explanation took 23.0 s and a longer one 74.9 s, so
    #: every local pass that was not trivial cut itself off at 30 s and came
    #: back as a 502. Callers that pass a timeout of their own still get
    #: exactly what they asked for; this only replaces the default, and only
    #: when the endpoint is one we host ourselves.
    LOCAL_DEFAULT_TIMEOUT = int(os.getenv('LLM_LOCAL_TIMEOUT', '240') or '240')
    DEFAULT_TEMPERATURE = 1.0
    DEFAULT_MAX_TOKENS = 0
    MAX_RETRIES = 3
    RETRY_DELAY = 0.5
    # Wall-clock ceiling on the whole retry sequence of one provider call
    # (src.retry_policy.RetryBudget), independent of MAX_RETRIES: a run of
    # long Retry-After waits could otherwise burn far more time than
    # MAX_RETRIES alone suggests. Override with env LLM_RETRY_TIME_BUDGET.
    RETRY_TIME_BUDGET = float(os.getenv('LLM_RETRY_TIME_BUDGET', '45') or '45')
    STREAM_TIMEOUT = 300
    # TCP+TLS connect budget for a SINGLE attempt. The old hard-coded 3.0s
    # assumed LAN/Tailscale peers ('SYN in <100ms'); it is too tight for public
    # cloud endpoints (offshore APIs take ~0.5-1.5s cold, with jitter), so a
    # brief blip on the first connect of an idle chat surfaced as a 503 on the
    # streaming path (which, unlike llm_call, does not retry the connect). A
    # genuinely dead upstream stays bounded by the dead-host cooldown. Override
    # with env LLM_CONNECT_TIMEOUT (seconds).
    CONNECT_TIMEOUT = float(os.getenv('LLM_CONNECT_TIMEOUT', '10') or '10')


class _FallbackIneligibleHTTPException(HTTPException):
    """HTTP-shaped provider failure that must never advance a route chain."""

    fallback_eligible = False


def _call_timeout(read_timeout) -> httpx.Timeout:
    """Per-request timeout for non-streaming LLM calls (connect from config)."""
    return httpx.Timeout(connect=LLMConfig.CONNECT_TIMEOUT, read=float(read_timeout), write=10.0, pool=5.0)


def _stream_timeout(read_timeout) -> httpx.Timeout:
    """Per-request timeout for streaming LLM calls (connect from config)."""
    return httpx.Timeout(connect=LLMConfig.CONNECT_TIMEOUT, read=float(read_timeout), write=30.0, pool=5.0)


# Cache for LLM responses
def _cache_header_identity(headers) -> str:
    """Return a non-secret identity for credential-distinct request routes."""

    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except (TypeError, ValueError, json.JSONDecodeError):
            headers = {"_raw": headers}
    if not isinstance(headers, dict):
        headers = {}
    canonical = [
        (str(key).strip().lower(), str(value))
        for key, value in headers.items()
    ]
    canonical.sort()
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _get_cache_key(url: str, model: str, messages: List[Dict],
                   temperature: float, max_tokens: int, headers=None,
                   response_schema=None) -> str:
    """Generate a cache key partitioned by endpoint and credential identity."""
    hashable_messages = []
    for msg in messages:
        sorted_items = tuple(sorted(msg.items()))
        hashable_messages.append(sorted_items)

    payload = {
        'url': url,
        'model': model,
        'messages': hashable_messages,
        'temp': temperature,
        'max_tokens': max_tokens,
        # Never put credentials in a cache key or loggable cache payload.  The
        # digest only prevents responses from one configured account/route
        # being returned under another route with the same URL and model.
        'header_identity': _cache_header_identity(headers),
    }
    if response_schema:
        # A constrained answer and a free-form one are different answers to
        # the same prompt; they must not share a cache entry. The field is
        # only added when a schema is really going out, so every key computed
        # before this existed keeps its exact digest.
        try:
            fingerprint = json.dumps(response_schema, sort_keys=True, default=str)
        except (TypeError, ValueError):
            fingerprint = repr(response_schema)
        payload['response_schema'] = hashlib.sha256(fingerprint.encode()).hexdigest()
    content = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()

_response_cache = {}
_response_model_cache = {}

# Dead-host cooldown: maps host (scheme://host:port) -> unix ts when cooldown expires.
# When a connect to a host fails, we mark it dead for DEAD_HOST_COOLDOWN seconds so
# subsequent calls fail instantly instead of waiting on the connect timeout. Keeps
# one unreachable upstream from jamming chat across the rest of the app.
#
# But a SINGLE transient blip (local model briefly busy, a momentary
# Tailscale hiccup) used to trip a full 60s lockout — the user saw a
# 503 and thought the model died when it was fine a second later. So:
#   - require FAIL_THRESHOLD consecutive failures before cooling
#   - shorter cooldown so recovery is quick
#   - any success resets the failure counter immediately
DEAD_HOST_COOLDOWN = 20.0
_HOST_FAIL_THRESHOLD = 2
_dead_hosts: Dict[str, float] = {}
_host_fails: Dict[str, int] = {}
# Guards the two maps above. The synchronous llm_call() runs inside FastAPI's
# threadpool (sync routes such as /sessions/auto-sort) while llm_call_async()
# runs on the event loop, so these maps are mutated from multiple OS threads.
# Without the lock the get()+1+set on _host_fails is a read-modify-write that
# loses failure counts under concurrent connect errors (issue #659).
_host_health_lock = threading.Lock()
_model_activity: Dict[str, float] = {}

_HARMONY_MARKER_RE = re.compile(
    r"<\|channel\|>(analysis|commentary|final)"
    r"|<\|start\|>(?:assistant|system|user|tool)?"
    r"|<\|message\|>"
    r"|<\|end\|>"
    r"|<\|return\|>"
    r"|<\|call\|>"
)
_HARMONY_MARKERS = (
    "<|channel|>analysis",
    "<|channel|>commentary",
    "<|channel|>final",
    "<|start|>assistant",
    "<|start|>system",
    "<|start|>user",
    "<|start|>tool",
    "<|start|>",
    "<|message|>",
    "<|end|>",
    "<|return|>",
    "<|call|>",
)
_HARMONY_MAX_MARKER_LEN = max(len(marker) for marker in _HARMONY_MARKERS)

_VISIBLE_CHAT_TEMPLATE_ARTIFACT_RE = re.compile(
    r"(?:\|end\|)+\|?assistan(?:t)?\|?"
    r"|\|assistan(?:t)?\|"
    r"|<\|im_start\|>\s*assistant"
    r"|<\|im_end\|>",
    re.IGNORECASE,
)


def _strip_visible_chat_template_artifacts(text: str) -> str:
    return _VISIBLE_CHAT_TEMPLATE_ARTIFACT_RE.sub("", text or "")


def _harmony_suffix_hold_len(text: str) -> int:
    """Return how many trailing chars could be the start of a harmony marker."""
    limit = min(len(text), _HARMONY_MAX_MARKER_LEN - 1)
    for n in range(limit, 0, -1):
        suffix = text[-n:]
        if any(marker.startswith(suffix) for marker in _HARMONY_MARKERS):
            return n
    return 0


class _HarmonyStreamRouter:
    """Route OpenAI harmony analysis/final channels without leaking markers."""

    def __init__(self) -> None:
        self._buf = ""
        self._seen_harmony = False
        self._channel: Optional[str] = None
        self._in_message = False

    def feed(self, text: str) -> List[Tuple[str, bool]]:
        if not text:
            return []
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> List[Tuple[str, bool]]:
        return self._drain(final=True)

    def _append_text(self, out: List[Tuple[str, bool]], text: str) -> None:
        if not text:
            return
        if not self._seen_harmony:
            out.append((text, False))
            return
        if self._in_message:
            # analysis + commentary (tool-call preambles / function-arg bodies)
            # are internal, not user-facing — route them to thinking so they
            # don't leak into the visible answer; only `final` is visible.
            out.append((text, self._channel in ("analysis", "commentary")))

    def _handle_marker(self, match: re.Match[str]) -> None:
        marker = match.group(0)
        self._seen_harmony = True
        if marker.startswith("<|channel|>"):
            self._channel = match.group(1)
            self._in_message = False
        elif marker == "<|message|>":
            self._in_message = True
        else:
            self._in_message = False
            if marker in {"<|end|>", "<|return|>", "<|call|>"}:
                self._channel = None

    def _drain(self, *, final: bool) -> List[Tuple[str, bool]]:
        out: List[Tuple[str, bool]] = []
        while True:
            match = _HARMONY_MARKER_RE.search(self._buf)
            if not match:
                break
            self._append_text(out, self._buf[:match.start()])
            self._handle_marker(match)
            self._buf = self._buf[match.end():]

        hold = 0 if final else _harmony_suffix_hold_len(self._buf)
        emit = self._buf if hold == 0 else self._buf[:-hold]
        self._buf = "" if hold == 0 else self._buf[-hold:]
        self._append_text(out, emit)
        return out


def _stream_delta_event(text: str, *, thinking: bool = False) -> str:
    payload = {"delta": text}
    if thinking:
        payload["thinking"] = True
    return f"data: {json.dumps(payload)}\n\n"


_DEGENERATE_WORD_RE = re.compile(r"[A-Za-z0-9_\u0370-\u03ff\u0400-\u04ff]+")

# Minimum span (characters) a raw repeated unit must cover before it counts
# as a degenerate collapse rather than legitimate repetitive-looking content
# (a long hash, a run of coincidentally-similar digits, an ASCII rule of
# dashes). Chosen so it never fires on ordinary prose but reliably beats
# Ollama's own abort, which on a local 27B model was observed only after a
# much longer runaway ("0000000000..." until Ollama itself returned
# "prediction aborted, token repeat limit reached", HTTP 400 mid-stream).
_DEGENERATE_RAW_REPEAT_MIN_CHARS = 120
# How much trailing raw text the guard keeps to look for a repeated unit.
# Must be >= min-chars + the longest unit checked, with slack for chunking.
_DEGENERATE_RAW_TAIL_WINDOW = 240
DEGENERATE_OUTPUT_ERROR_CLASS = "degenerate_output"

# Gibberish-script guard defaults (overridable via `local_gibberish_*`
# settings — see settings.py for the rationale). Catches a collapse into a
# run of a DIFFERENT script each token (Cyrillic, CJK, ...) that never
# repeats one short unit and so slips past the repeat-based checks above —
# seen live: "lis the lis the" degrading into sustained Cyrillic runs with no
# Ollama repeat-limit abort at all.
_GIBBERISH_WINDOW_CHARS_DEFAULT = 300
_GIBBERISH_THRESHOLD_DEFAULT = 0.40


def _unexpected_script_fraction(text: str) -> float:
    """Fraction of the ALPHABETIC characters in `text` that are not Latin
    script. Digits, punctuation, whitespace and emoji are ignored entirely
    (they are "expected" in every language and would only dilute the
    signal); an accented Latin letter (`unicodedata.name` starting
    "LATIN...") counts as expected."""
    if not text:
        return 0.0
    unexpected = 0
    counted = 0
    for ch in text:
        if not ch.isalpha():
            continue
        counted += 1
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue  # unnamed codepoint — never penalize what we can't classify
        if not name.startswith("LATIN"):
            unexpected += 1
    return (unexpected / counted) if counted else 0.0


_SCRIPT_GROUPS = (
    ("LATIN", "LATIN"),
    ("CJK", "CJK"), ("HIRAGANA", "CJK"), ("KATAKANA", "CJK"), ("HANGUL", "CJK"),
    ("HALFWIDTH", "CJK"), ("FULLWIDTH", "CJK"),
    ("CYRILLIC", "CYRILLIC"), ("GREEK", "GREEK"), ("ARABIC", "ARABIC"),
    ("HEBREW", "HEBREW"), ("THAI", "THAI"), ("DEVANAGARI", "DEVANAGARI"),
    ("BENGALI", "BENGALI"), ("TAMIL", "TAMIL"), ("GEORGIAN", "GEORGIAN"),
    ("ARMENIAN", "ARMENIAN"), ("ETHIOPIC", "ETHIOPIC"), ("KHMER", "KHMER"),
)


def _script_class(ch: str) -> str:
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return ""
    for prefix, group in _SCRIPT_GROUPS:
        if name.startswith(prefix):
            return group
    return "OTHER"


def _token_soup_reason(text: str) -> Optional[str]:
    """Word salad across writing systems: the output of a model server whose
    state is broken, not of a model thinking in some language.

    24-09-2026, llama-server after another process loaded a large model on
    the same GPUs: fragments such as «orque多大wanysofarotre成果 compens狼癞»
    from the first token, Chinese, Thai, Cyrillic, Arabic and Katakana glued
    to Latin fragments with almost no spaces. Only 14% of the letters were
    non-Latin, so the script-fraction check never fired, and the turn spent
    fifteen minutes on it before the server answered HTTP 500.

    Two signals, each rare in real text: three or more non-Latin writing
    systems inside one window (a Chinese/English or Japanese/English mix
    has one), or a dozen script switches in a window with hardly any
    whitespace (prose, code and JSON all have spaces or newlines).
    """
    classes = []
    whitespace = 0
    for ch in text:
        if ch.isspace():
            whitespace += 1
            continue
        if not ch.isalpha():
            continue
        cls = _script_class(ch)
        if cls:
            classes.append(cls)
    if len(classes) < 40:
        return None
    foreign = {c for c in classes if c not in ("LATIN", "OTHER")}
    switches = sum(1 for a, b in zip(classes, classes[1:]) if a != b)
    ws_ratio = whitespace / max(len(text), 1)
    if len(foreign) >= 3:
        return (f"word salad across {len(foreign) + 1} writing systems "
                f"({', '.join(sorted(foreign))} mixed into Latin)")
    if switches >= 12 and ws_ratio < 0.06:
        return f"word salad: {switches} script switches with {ws_ratio:.0%} whitespace"
    return None


def _gibberish_guard_settings() -> Tuple[int, float]:
    """(window_chars, threshold) from settings, falling back safely."""
    try:
        from src.settings import get_setting
        window = int(get_setting("local_gibberish_window_chars", _GIBBERISH_WINDOW_CHARS_DEFAULT))
    except Exception:
        window = _GIBBERISH_WINDOW_CHARS_DEFAULT
    try:
        from src.settings import get_setting
        threshold = float(get_setting("local_gibberish_script_threshold", _GIBBERISH_THRESHOLD_DEFAULT))
    except Exception:
        threshold = _GIBBERISH_THRESHOLD_DEFAULT
    if window <= 0:
        window = _GIBBERISH_WINDOW_CHARS_DEFAULT
    if not (0.0 < threshold <= 1.0):
        threshold = _GIBBERISH_THRESHOLD_DEFAULT
    return window, threshold


class DegenerateOutput(Exception):
    """Raised by `_DegenerateStreamGuard.check` when the model's streamed
    output has collapsed into a repeated character/token/phrase loop.

    Raised client-side, mid-stream, the moment the collapse is recognized \u2014
    deliberately not waiting for the provider's own error. Ollama's native
    "prediction aborted, token repeat limit reached" (HTTP 400) only arrives
    after a much longer run of degenerate output on a local model, and by
    then a lot of "0000000000\u2026" has already reached the UI and the context
    window. Carries `reason` (a short human-readable description, safe to
    show) and `model` for logging/telemetry.
    """

    def __init__(self, reason: str, model: str = ""):
        self.reason = reason
        self.model = model or "model"
        super().__init__(reason)


def is_degenerate_output_error(error_data) -> bool:
    """True when a stream error is a token-repeat collapse \u2014 either this
    runtime's own client-side guard (`DegenerateOutput`/`error_class`) or
    Ollama's own native abort message ("prediction aborted, token repeat
    limit reached", HTTP 400 mid-stream)."""
    if not isinstance(error_data, dict):
        return False
    if str(error_data.get("error_class") or "") == DEGENERATE_OUTPUT_ERROR_CLASS:
        return True
    return "token repeat limit" in str(error_data.get("error") or "").lower()


def _degenerate_output_error_chunk(exc: "DegenerateOutput") -> str:
    """Typed `event: error` SSE chunk for a `DegenerateOutput` abort, tagged
    with `error_class` so `is_degenerate_output_error` (and the agent
    harness's targeted retry) can tell it apart from an ordinary transport
    failure without string-matching the human-readable message."""
    logger.warning("[degenerate-stream] aborting model=%s reason=%s", exc.model, exc.reason)
    message = (
        f"Stopped generation: {exc.model} started repeating tokens "
        f"({exc.reason}). Try a different model or lower temperature."
    )
    return f'event: error\ndata: {json.dumps({"status": 502, "text": message, "error": message, "error_class": DEGENERATE_OUTPUT_ERROR_CLASS, "fallback_eligible": False})}\n\n'


#: Tool-call argument loop guard (see `tool_argument_loop`): the repeated unit
#: must be at least this long, repeat back-to-back this many times at the very
#: end of the arguments, and read like prose (this many alphabetic words).
_TOOL_ARG_LOOP_MIN_UNIT = 40
_TOOL_ARG_LOOP_MAX_UNIT = 600
_TOOL_ARG_LOOP_MIN_REPEATS = 8
_TOOL_ARG_LOOP_MIN_WORDS = 5
#: Only look once the arguments are this long, and again every this many chars.
_TOOL_ARG_LOOP_START_CHARS = 1500
_TOOL_ARG_LOOP_EVERY_CHARS = 400
_ALPHA_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def tool_argument_loop(arguments: str) -> Optional[str]:
    """A tool call's streamed arguments stuck in a loop, or None.

    Seen live on a local 27B behind llama-server: a `rationale` field
    repeated one Spanish sentence until max_tokens -- 8192 tokens, 14 minutes
    -- and nothing reached the screen because a server-side tool-call parser
    holds argument text until the call is complete, so the content and
    reasoning guards never saw it. Deliberately strict so a file that really
    repeats a line never trips it: the unit is 40-600 characters of prose
    (5+ words), repeated back-to-back at least 8 times at the very end."""
    text = arguments or ""
    probe_len = _TOOL_ARG_LOOP_MIN_UNIT
    if len(text) < probe_len * _TOOL_ARG_LOOP_MIN_REPEATS:
        return None
    probe = text[-probe_len:]
    prev = text.rfind(probe, 0, len(text) - probe_len)
    if prev < 0:
        return None
    unit_len = len(text) - probe_len - prev
    if not (_TOOL_ARG_LOOP_MIN_UNIT <= unit_len <= _TOOL_ARG_LOOP_MAX_UNIT):
        return None
    unit = text[-unit_len:]
    if len(_ALPHA_WORD_RE.findall(unit)) < _TOOL_ARG_LOOP_MIN_WORDS:
        return None
    repeats, end = 0, len(text)
    while end - unit_len >= 0 and text[end - unit_len:end] == unit:
        repeats += 1
        end -= unit_len
    if repeats < _TOOL_ARG_LOOP_MIN_REPEATS:
        return None
    return f"tool-call arguments repeat a {unit_len}-char passage {repeats} times"


#: Template-loop guard (see `tool_argument_template_loop`).
_TEMPLATE_LOOP_MIN_CHARS = 3000
_TEMPLATE_LOOP_WINDOW = 40
_TEMPLATE_LOOP_MIN_LINES = 24
_TEMPLATE_LOOP_MAX_SKELETONS = 4
_TEMPLATE_LOOP_MIN_WORDS = 2
_QUOTED_RE = re.compile(r'"[^"]{0,400}"|\u201c[^\u201d]{0,400}\u201d')
_DIGITS_RE = re.compile(r"\d+")
_COMMENT_START_RE = re.compile(r"^\s*(?:#|//|--|;|\*)")
_CODE_PUNCT_RE = re.compile(r"[(\[{=]")


def _line_skeleton(line: str) -> str:
    line = line.replace('\\"', '"')  # arguments are JSON text: \" is a quote
    return _DIGITS_RE.sub("0", _QUOTED_RE.sub("Q", line)).strip()


def tool_argument_template_loop(arguments: str) -> Optional[str]:
    """Tool-call arguments cycling through one sentence template, or None.

    Live, 24-09-2026 (exam run 15): a local 27B tried to recall a poem from
    memory inside a python comment — "-> It is from <poem>: <line> NO." —
    one new quoted line after another, 8192 tokens and 14 minutes, never
    the same line twice in a row, so the exact-repeat guard above never
    matched. With the quoted parts and numbers masked, the last lines of
    prose collapse to two or three skeletons. Data rows (`"k": "v",`,
    `print("...")`) have no prose left once masked and are not counted."""
    text = arguments or ""
    if len(text) < _TEMPLATE_LOOP_MIN_CHARS:
        return None
    lines = [ln for ln in re.split(r"\\n|\n", text) if ln.strip()]
    # The loop must be what the arguments END with: look only at the last
    # lines, and stop at the first line that is neither prose nor blank-ish
    # (a legitimate block of code or data after some repeated prose is not
    # a loop in progress).
    skeletons: list = []
    raw_tail: list = []
    for ln in reversed(lines[-(_TEMPLATE_LOOP_WINDOW * 3):]):
        sk = _line_skeleton(ln)
        if len(_ALPHA_WORD_RE.findall(sk)) < _TEMPLATE_LOOP_MIN_WORDS:
            if len(ln.strip()) <= 3:
                continue  # a closing bracket or an empty line
            break  # a data row: the arguments end in data, not in a loop
        # Prose only: a comment, or a line with no code punctuation. Forty
        # `print(f"...")` lines are a legitimate script, not a loop.
        if not (_COMMENT_START_RE.match(sk) or not _CODE_PUNCT_RE.search(sk)):
            break
        skeletons.append(sk)
        raw_tail.append(ln)
        if len(skeletons) >= _TEMPLATE_LOOP_WINDOW:
            break
    tail = skeletons
    if len(tail) < _TEMPLATE_LOOP_MIN_LINES:
        return None
    distinct = len(set(tail))
    if distinct > _TEMPLATE_LOOP_MAX_SKELETONS:
        return None
    if len(set(ln.strip() for ln in raw_tail)) <= distinct:
        return None  # identical lines: the exact-repeat guard's case, not this one
    return (f"tool-call arguments cycle through {distinct} sentence template(s) "
            f"over their last {len(tail)} lines")


class _DegenerateStreamGuard:
    """Detect local-model token collapse before it floods the UI.

    Some self-hosted models fail by repeating one token forever ("Var Var Var",
    "Summer Summer ..."), or \u2014 with the default Ollama sampler (repeat_penalty
    1.0, min_p 0) on a fresh session \u2014 by decoding into a single repeated
    character or very short unit forever ("0000000000\u2026"). This is not a
    useful response and can burn context, browser memory, and GPU time. Keep
    the guard conservative: only fire on long same-token runs, a very
    dominant repeated token in the recent window, or >= 120 raw characters of
    a repeated <=4-char unit \u2014 never on legitimately repetitive-looking
    content like a long hash or a decimal expansion, which do not consist of
    one small unit repeated over that whole span.
    """

    def __init__(self, model: str):
        self.model = model or "model"
        self.last_token = ""
        self.same_run = 0
        self.recent_tokens: List[str] = []
        self.total_chars = 0
        self.tail = ""
        self.script_tail = ""
        self._gibberish_window, self._gibberish_threshold = _gibberish_guard_settings()

    def _raw_repeat_reason(self) -> Optional[str]:
        t = self.tail
        if len(t) < _DEGENERATE_RAW_REPEAT_MIN_CHARS:
            return None
        for unit_len in range(1, 5):
            unit = t[-unit_len:]
            if not unit.strip():
                continue
            count = 0
            i = len(t)
            while i - unit_len >= 0 and t[i - unit_len:i] == unit:
                count += 1
                i -= unit_len
            span = count * unit_len
            if span >= _DEGENERATE_RAW_REPEAT_MIN_CHARS:
                return f"repeated unit {unit!r} {count} times ({span} chars)"
        return None

    def check(self, text: str) -> None:
        """Feed the next streamed chunk of text. Raises `DegenerateOutput`
        the moment a collapse is recognized; returns normally otherwise."""
        if not text:
            return
        self.total_chars += len(text)
        self.tail = (self.tail + text)[-_DEGENERATE_RAW_TAIL_WINDOW:]

        reason = self._raw_repeat_reason()

        tokens = [t.lower() for t in _DEGENERATE_WORD_RE.findall(text) if len(t) >= 2]
        for token in tokens:
            if token == self.last_token:
                self.same_run += 1
            else:
                self.last_token = token
                self.same_run = 1
            self.recent_tokens.append(token)
        if len(self.recent_tokens) > 96:
            self.recent_tokens = self.recent_tokens[-96:]

        if not reason and self.same_run >= 28 and self.total_chars >= 100:
            reason = f"repeated '{self.last_token}' {self.same_run} times"
        elif not reason and len(self.recent_tokens) >= 72:
            top = max(set(self.recent_tokens), key=self.recent_tokens.count)
            count = self.recent_tokens.count(top)
            if count >= 60 and count / max(len(self.recent_tokens), 1) >= 0.78:
                reason = f"repeated '{top}' {count}/{len(self.recent_tokens)} recent tokens"
        if not reason and len(self.recent_tokens) >= 80:
            # Phrase loops are common on some local quantized MLX/MoE models:
            # "Also be a software developer mode?" repeated forever will not
            # trip the single-token guard above, but it is still a wedged
            # generation. Require many repeats of the same 4-gram so normal
            # prose/list formatting is not interrupted.
            grams = [tuple(self.recent_tokens[i:i + 4]) for i in range(0, len(self.recent_tokens) - 3)]
            if grams:
                top_gram = max(set(grams), key=grams.count)
                gram_count = grams.count(top_gram)
                if gram_count >= 10:
                    reason = f"repeated phrase '{' '.join(top_gram)}' {gram_count} times"

        if not reason:
            # Pure gibberish that never repeats one short unit — a run of
            # Cyrillic/CJK/etc. tokens on what should be a Latin-script
            # (Spanish/English) conversation. Only evaluated once the window
            # is full, so a short foreign quote inside an otherwise-Latin
            # answer cannot trip it (spec: never on legitimately
            # multilingual content shorter than the window).
            self.script_tail = (self.script_tail + text)[-self._gibberish_window:]
            if len(self.script_tail) >= self._gibberish_window:
                frac = _unexpected_script_fraction(self.script_tail)
                if frac > self._gibberish_threshold:
                    reason = (
                        f"{frac:.0%} non-Latin characters over the last "
                        f"{len(self.script_tail)} chars"
                    )
                else:
                    reason = _token_soup_reason(self.script_tail)
                if reason and self.total_chars <= self._gibberish_window * 2:
                    # Garbage from the very first tokens is the server, not
                    # the conversation: say what usually fixes it.
                    reason += (" from the first tokens; the model server itself is probably "
                               "in a broken state, and restarting it usually fixes this")

        if reason:
            raise DegenerateOutput(reason, self.model)

    def check_reasoning(self, text: str) -> None:
        """`check` plus a paragraph-loop detector for the reasoning channel.

        20-09-2026, qwen3.8 27B q4 on a batch task: the thinking restated the
        same five paragraphs verbatim four or five times until the reasoning
        budget cut it (~250 s of GPU for nothing). Each cycle is hundreds of
        tokens long, far beyond the 96-token window above, so none of the
        token/phrase rules could see it. A long sentence (>= 60 chars) that
        comes back word for word a third time is a wedged plan, not thought.
        Reasoning only: content can legitimately repeat long lines (tables,
        code, JSON rows)."""
        self.check(text)
        if not text:
            return
        buf = getattr(self, "_sentence_buf", "") + text
        parts = _REASONING_SENTENCE_SPLIT_RE.split(buf)
        self._sentence_buf = parts.pop() if parts else ""
        if len(self._sentence_buf) > 4000:
            self._sentence_buf = self._sentence_buf[-4000:]
        counts = getattr(self, "_sentence_counts", None)
        if counts is None:
            counts = self._sentence_counts = {}
        for sentence in parts:
            norm = " ".join(sentence.lower().split())
            if len(norm) < _REASONING_LOOP_MIN_CHARS:
                continue
            counts[norm] = counts.get(norm, 0) + 1
            if counts[norm] >= _REASONING_LOOP_REPEATS:
                raise DegenerateOutput(
                    f"reasoning loop: the same sentence came back {counts[norm]} times "
                    f"('{norm[:60]}…')",
                    self.model,
                )
        if len(counts) > 4000:
            for key in list(counts)[:2000]:
                counts.pop(key, None)


_REASONING_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_REASONING_LOOP_MIN_CHARS = 60
_REASONING_LOOP_REPEATS = 3


def _model_activity_key(url: str, model: str) -> str:
    return f"{(url or '').strip()}|{(model or '').strip()}"

def _same_model_identity(left: str, right: str) -> bool:
    return (left or "").strip().lower() == (right or "").strip().lower()

def _reported_model_name(value) -> str:
    """Return a provider model identifier only when it is usable metadata."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _model_actual_event(requested_model: str, reported_model) -> Optional[str]:
    """Build a provenance event when a provider resolves a different model."""
    actual_model = _reported_model_name(reported_model)
    if not actual_model or _same_model_identity(actual_model, requested_model):
        return None
    return f'data: {json.dumps({"type": "model_actual", "requested_model": requested_model, "model": actual_model})}\n\n'


def _annotate_usage_model(usage: dict, requested_model: str, actual_model: str) -> dict:
    """Attach provider model provenance to a normalized usage payload."""
    actual_model = _reported_model_name(actual_model)
    if actual_model:
        usage["model"] = actual_model
        if not _same_model_identity(actual_model, requested_model):
            usage["requested_model"] = requested_model
    return usage


def note_model_activity(url: str, model: str):
    """Record that a real upstream request used this endpoint/model."""
    if not url or not model:
        return
    _model_activity[_model_activity_key(url, model)] = time.time()

def seconds_since_model_activity(url: str, model: str) -> Optional[float]:
    """Seconds since the endpoint/model was last used in this process."""
    ts = _model_activity.get(_model_activity_key(url, model))
    if not ts:
        return None
    return max(0.0, time.time() - ts)

def _host_key(url: str) -> str:
    from urllib.parse import urlsplit
    s = urlsplit(url)
    return f"{s.scheme}://{s.netloc}" if s.scheme and s.netloc else url

def _is_host_dead(url: str) -> bool:
    key = _host_key(url)
    with _host_health_lock:
        exp = _dead_hosts.get(key)
        if exp is None:
            return False
        if time.time() >= exp:
            _dead_hosts.pop(key, None)
            return False
        return True

def _mark_host_dead(url: str) -> bool:
    """Record a connect failure. Only actually cools the host after
    _HOST_FAIL_THRESHOLD consecutive failures. Returns True if the host
    is now cooled (so callers can log accurately), False if it's still
    within its allowed-failure grace."""
    key = _host_key(url)
    with _host_health_lock:
        n = _host_fails.get(key, 0) + 1
        _host_fails[key] = n
        if n >= _HOST_FAIL_THRESHOLD:
            _dead_hosts[key] = time.time() + DEAD_HOST_COOLDOWN
            return True
        return False

def _clear_host_dead(url: str) -> None:
    key = _host_key(url)
    with _host_health_lock:
        _dead_hosts.pop(key, None)
        _host_fails.pop(key, None)


# Shared async HTTP client. Reusing one client keeps connections warm:
# repeat calls to api.anthropic.com / api.openai.com / openrouter skip the
# 100-500ms TCP+TLS handshake. Lazy init so we bind to the running event loop.
#
# A pooled connection belongs to the event loop that opened it. Background
# jobs (context maintenance, the second brain's extraction and wiki passes,
# typed decisions from sync code) run `asyncio.run` in a worker thread: when
# such a job was the first caller, the shared client's connections were bound
# to that short-lived loop, the loop closed, and the next chat turn on the app
# loop failed at once with "Event loop is closed" (reported as a 502
# transport error). So the shared client remembers the loop that created it:
# a second live loop gets a client of its own, and a client whose loop is
# gone is replaced instead of reused.
_http_client: Optional[httpx.AsyncClient] = None
_http_client_owner: Optional[Tuple[int, Any]] = None  # (id(client), loop that created it)
_loop_http_clients: "weakref.WeakKeyDictionary[Any, httpx.AsyncClient]" = weakref.WeakKeyDictionary()
_http_limits = httpx.Limits(max_connections=100, max_keepalive_connections=30, keepalive_expiry=30.0)


def _new_http_client() -> httpx.AsyncClient:
    from src.tls_overrides import llm_verify
    return httpx.AsyncClient(limits=_http_limits, http2=False, verify=llm_verify())


def _get_http_client() -> httpx.AsyncClient:
    """Return the AsyncClient for the running event loop. Per-request timeout
    is passed at call time."""
    global _http_client, _http_client_owner
    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    client = _http_client
    if client is not None and not client.is_closed:
        owner = _http_client_owner
        if owner is None or owner[0] != id(client):
            # Set from outside (tests) or before this bookkeeping existed:
            # adopt it for the current loop.
            _http_client_owner = (id(client), loop)
            return client
        owner_loop = owner[1]
        if owner_loop is None or owner_loop is loop or loop is None:
            return client
        if not owner_loop.is_closed():
            # Another loop is alive and owns the shared client (a worker
            # thread's asyncio.run): this loop gets its own.
            own = _loop_http_clients.get(loop)
            if own is None or own.is_closed:
                own = _new_http_client()
                _loop_http_clients[loop] = own
            return own
        # The owner loop is gone: its pooled connections are unusable.
    _http_client = _new_http_client()
    _http_client_owner = (id(_http_client), loop)
    return _http_client

def _get_cached_response(cache_key: str) -> Optional[str]:
    """Get cached response if it exists."""
    return _response_cache.get(cache_key)


def _get_cached_response_model(cache_key: str) -> Optional[str]:
    """Return provider-reported model metadata paired with a cached reply."""
    model = _response_model_cache.get(cache_key)
    return model if isinstance(model, str) and model.strip() else None


#: Characters that make a completion an answer. The letter range covers
#: accented Latin, Greek, Cyrillic, CJK and the rest deliberately: "no
#: letters" has to mean no letters, not "no ASCII letters", or every answer in
#: Chinese would be thrown away. Braces and brackets count too: `{}` is a
#: complete and legitimate reply to a request that asked for JSON, and the
#: first version of this rule threw it away as punctuation.
_ANSWER_RE = re.compile(r"[0-9A-Za-z{}\[\]À-ɏͰ-῿぀-퟿]")

#: Below this, a SINGLE-WORD reasoning channel is a leftover rather than an
#: answer. A healthy model that puts its answer there writes sentences; a
#: backend whose slot has gone bad emits a handful of characters -- measured
#: on this machine: content "", reasoning "/umd``", three tokens, 0.8 s, the
#: same bytes on every request while the prompt cache kept growing.
#:
#: Length alone was the first rule and it was too blunt: it threw away short
#: but perfectly well-formed answers ("Sí, ya está arreglado."), which
#: tests/test_llm_core_reasoning_content_fallback.py caught. What separates
#: "/umd``" from a real reply is not length but shape -- a reply is words
#: with spaces between them. So: several words is an answer at any length,
#: and a single token has to be long enough to be a sentence's worth.
MIN_REASONING_AS_ANSWER = 40

#: Runs of letters or digits. A phrase is whitespace plus two or more of
#: these -- kept as two cheap checks rather than one clever pattern, because
#: "2 + 2 = 4" separates its words with punctuation and a single
#: `\s`-joined pattern quietly rejected it.
_REASONING_WORD_RE = re.compile(r"[0-9A-Za-zÀ-ɏͰ-῿぀-퟿]+")


def _reads_like_a_phrase(text: str) -> bool:
    """Whitespace, and at least two words around it."""
    return any(ch.isspace() for ch in text) and len(_REASONING_WORD_RE.findall(text)) >= 2


def empty_completion(text: Optional[str]) -> bool:
    """Is this a completion with nothing in it?

    True for an empty or whitespace-only answer, and for one with no letter or
    digit anywhere -- a run of punctuation is not an answer in any language.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return True
    return _ANSWER_RE.search(stripped) is None


def reasoning_as_answer(reasoning: Optional[str]) -> str:
    """The reasoning channel, but only when it reads like a reply.

    Falling back to the reasoning channel exists for models that put their
    whole answer there. Taking it unconditionally is how six characters of
    punctuation from a sick engine reached the user as though the model had
    said it.

    A phrase -- two or more words with whitespace between them -- is a reply
    at any length; "Yes, fixed." is a complete answer. A single unbroken
    token has to reach `MIN_REASONING_AS_ANSWER` before it counts, which is
    what keeps "/umd``" out without throwing away short real answers.
    """
    text = str(reasoning or "").strip()
    if empty_completion(text):
        return ""
    if not _reads_like_a_phrase(text) and len(text) < MIN_REASONING_AS_ANSWER:
        return ""
    return text


def _set_cached_response(
    cache_key: str,
    response: str,
    *,
    actual_model: Optional[str] = None,
) -> None:
    """Store response in cache.

    An empty completion is never stored. It is a symptom of a backend in a bad
    state, and caching it turns a transient fault into a permanent one: the
    same question then answers with the same nothing without the model being
    asked at all.
    """
    if empty_completion(response):
        logger.warning("Not caching an empty completion (backend returned nothing)")
        return
    if len(_response_cache) > 128:
        keys_to_remove = list(_response_cache.keys())[:64]
        for key in keys_to_remove:
            # pop(), not del: another thread (sync llm_call runs in FastAPI's
            # threadpool) may have already evicted the same snapshotted key,
            # and del would raise KeyError mid-eviction (issue #659).
            _response_cache.pop(key, None)
            _response_model_cache.pop(key, None)
    _response_cache[cache_key] = response
    if isinstance(actual_model, str) and actual_model.strip():
        _response_model_cache[cache_key] = actual_model.strip()
    else:
        _response_model_cache.pop(cache_key, None)

# ── Anthropic native API adapter ──

ANTHROPIC_MODELS = [
    "claude-opus-4-20250514", "claude-opus-4",
    "claude-sonnet-4-20250514", "claude-sonnet-4", "claude-sonnet-4-5-20250929", "claude-sonnet-4-5",
    "claude-haiku-4-20250514", "claude-haiku-4", "claude-haiku-3-5-20241022", "claude-haiku-3-5",
]


def _is_ollama_native_url(url: str) -> bool:
    """Return True for native Ollama API URLs, including Ollama Cloud."""
    try:
        parsed = urlparse(url or "")
    except Exception as e:
        logger.warning("Failed to parse URL for Ollama detection", exc_info=e)
        return False
    host = parsed.hostname or ""
    path = (parsed.path or "").rstrip("/")
    if _host_match(url, "ollama.com"):
        return True
    if path.startswith("/v1"):
        return False
    if not (path == "" or path == "/api" or path.startswith("/api/")):
        return False
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or parsed.port == 11434:
        return True
    # Off the default port and not loopback: Ollama only when the admin said
    # so by saving load options for that server in Settings → Local models
    # (the page lists any "ollama" host) — that is where the /v1 → /api/chat
    # reroute of `_route_for_gen_overrides` sends such a request.
    return _is_declared_ollama_host(url)


def _is_loopback_url(url: str) -> bool:
    """True when `url`'s host is this machine (localhost/127.0.0.1/::1/
    0.0.0.0) regardless of port — used to gate provider-agnostic local-only
    behaviour (e.g. forwarding llama.cpp sampler fields to a non-Ollama
    OpenAI-compatible server) that would be unsafe to guess for a remote
    provider."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def _is_declared_ollama_host(url: str) -> bool:
    try:
        from src.model_load_options import is_declared_ollama_host
        return is_declared_ollama_host(url)
    except Exception:  # noqa: BLE001 — never worth failing a request
        return False


def _is_local_ollama_target(url: str) -> bool:
    """True when `url` is definitively a local Ollama server for the
    purposes of applying Ollama-specific behaviour (the sampler floor, the
    /v1 -> native reroute) WITHOUT an explicit admin declaration: the
    default port (11434) on Ollama's own OpenAI-compatible surface, or a
    host/port the admin declared by saving Local models options for it.

    Deliberately narrower than `_is_ollama_openai_compat_url` (which — like
    `_is_ollama_native_url` — treats ANY loopback host as Ollama regardless
    of port, matching Ollama's own default listen address): an arbitrary
    loopback dev server on some other port must not be silently assumed to
    be Ollama and have Ollama-only fields injected into its requests just
    because it also serves an OpenAI-shaped /v1 path.
    """
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    if port == 11434 and _is_ollama_openai_compat_url(url):
        return True
    return _is_declared_ollama_host(url)


def _is_ollama_openai_compat_url(url: str) -> bool:
    """Return True for local Ollama's OpenAI-compatible /v1 surface.

    Mirrors the host detection used by ``_is_ollama_native_url`` so that the
    two helpers stay in lockstep: a localhost Ollama on a non-default port
    (custom ``OLLAMA_HOST``, reverse proxy, container port remap) is treated
    the same way here as it is on the native ``/api`` path.
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    host = parsed.hostname or ""
    path = (parsed.path or "").rstrip("/")
    local_ollama_host = host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or parsed.port == 11434
    return local_ollama_host and (path == "/v1" or path.startswith("/v1/"))


def _ollama_api_root(url: str) -> str:
    """Return a native Ollama API root such as https://ollama.com/api."""
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/api/chat"):
        return url[: -len("/chat")]
    if path.endswith("/api/tags"):
        return url[: -len("/tags")]
    if path.endswith("/api/generate"):
        return url[: -len("/generate")]
    if path.endswith("/api"):
        return url
    if path == "":
        return url + "/api"
    if _host_match(url, "ollama.com"):
        root = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else "https://ollama.com"
        return root.rstrip("/") + "/api"
    return url


def _normalize_ollama_url(url: str) -> str:
    """Ensure a native Ollama URL points at /api/chat."""
    base = _ollama_api_root(url)
    return base.rstrip("/") + "/chat"


def _normalize_openai_chat_url(url: str) -> str:
    """Ensure an OpenAI-compatible base URL points at /chat/completions."""
    base = (url or "").strip().rstrip("/")
    if not base:
        return base
    if base.endswith("/chat/completions") or base.endswith("/completions"):
        return base
    if base.endswith("/models"):
        base = base[: -len("/models")].rstrip("/")
    return base + "/chat/completions"


def _ollama_normalize_messages(messages: List[Dict]) -> List[Dict]:
    """Adapt Faustus' canonical OpenAI-style messages to native Ollama /api/chat.

    Two shape mismatches silently break requests:

    1. Tool calls: Faustus carries `function.arguments` as a JSON *string*.
       Native Ollama expects a JSON *object* and rejects the string form with
       HTTP 400 ("Value looks like object, but can't find closing '}' symbol"),
       aborting every follow-up (tool-result) round. Parse the arguments back
       into an object here, on a shallow copy, leaving non-tool messages
       untouched. The opaque Gemini `extra_content` (thought_signature) is
       dropped — it is meaningless to Ollama and only matters when the
       conversation is replayed to Gemini.

    2. Images (issue #4723): Faustus carries multimodal user content as an
       OpenAI-style list ``[{type: "text", ...}, {type: "image_url",
       image_url: {url: "data:image/...;base64,XXX"}}, ...]``. Native Ollama
       does not accept a list for ``content`` — it wants ``content`` as a
       string plus a separate ``images`` array of raw base64 strings (no
       ``data:`` prefix). Without this conversion the image blocks pass
       through untouched, the vision-capable model never sees the picture,
       and the user gets "I can't see any image" even though the request
       succeeded.
    """
    out: List[Dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            out.append(m)
            continue

        nm = dict(m)

        # 1. Tool-call argument strings -> objects.
        tcs = nm.get("tool_calls")
        if tcs:
            new_calls = []
            for tc in tcs:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                call: Dict = {"function": {"name": fn.get("name", ""), "arguments": args or {}}}
                if tc.get("id"):
                    call["id"] = tc["id"]
                new_calls.append(call)
            nm["tool_calls"] = new_calls

        # 2. Multimodal content list -> native content string + images array.
        content = nm.get("content")
        if isinstance(content, list):
            text_parts: List[str] = []
            images: List[str] = list(nm.get("images") or [])
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    t = block.get("text")
                    if t:
                        text_parts.append(str(t))
                elif btype == "image_url":
                    url = (block.get("image_url") or {}).get("url", "")
                    if not url:
                        continue
                    if url.startswith("data:"):
                        # Strip the ``data:[...];base64,`` prefix — native
                        # Ollama wants only the base64 bytes.
                        _, _, b64 = url.partition(",")
                        if b64:
                            images.append(b64)
                    else:
                        # Native Ollama images[] is base64-only; it does
                        # not fetch HTTP URLs.  Skip unsupported schemes
                        # rather than sending a non-base64 string that the
                        # model silently ignores.
                        logger.warning(
                            "Skipping non-data image_url (Ollama images[] "
                            "requires base64): %s",
                            url[:80],
                        )
            nm["content"] = "\n".join(text_parts).strip()
            if images:
                nm["images"] = images

        out.append(nm)
    return out


# Backward-compatible alias for callers/tests that imported the older name
# (it only handled tool messages originally — issue #4723 broadened scope).
_ollama_normalize_tool_messages = _ollama_normalize_messages


def _build_ollama_payload(
    model: str,
    messages: List[Dict],
    temperature: float,
    max_tokens: int,
    stream: bool = False,
    tools: Optional[List[Dict]] = None,
    num_ctx: Optional[int] = None,
    response_schema: Optional[Dict] = None,
) -> Dict:
    """Build the JSON payload for Ollama's /api/chat endpoint.

    ``num_ctx`` sets the input context window. Ollama defaults to 2048
    when the option is omitted, so a model with a larger advertised
    window is silently truncated there, and a model with a smaller one
    gets an oversized window it can't service. Pass the discovered
    context length through ``num_ctx``; this builder only emits it when
    the value is trusted (not the ``DEFAULT_CONTEXT`` fallback), so we
    don't guess for unknown models but do tell Ollama the real window
    when we know it — even if it's smaller than 2048.

    ``response_schema`` is a JSON Schema for Ollama's native ``format``
    parameter: the server compiles it into a grammar and masks the logits,
    so the model *cannot* emit a token that breaks the schema. It is only
    ever emitted for a tool-less request — see ``_resolve_response_schema``
    for why, and for the endpoint gate that decides whether a caller's
    schema reaches this builder at all.
    """
    payload: Dict = {
        "model": model,
        "messages": _ollama_normalize_messages(messages),
        "stream": stream,
    }
    options: Dict = {}
    if temperature is not None:
        options["temperature"] = temperature
    if max_tokens and max_tokens > 0:
        options["num_predict"] = max_tokens
    if num_ctx is not None and num_ctx > 0 and num_ctx != DEFAULT_CONTEXT:
        options["num_ctx"] = num_ctx
    if options:
        payload["options"] = options
    if tools:
        payload["tools"] = _alias_harmony_tools(tools, model)
    if response_schema:
        if tools:
            # Ollama does not combine `format` with `tools` reliably across
            # versions (the grammar and the tool-call decoder fight over the
            # same output): a request that carries both can come back with an
            # empty message. Constrained decoding is for the tool-less passes
            # only, so drop the schema rather than risk the tool call.
            logger.debug(
                "Not sending Ollama `format` alongside `tools` for %s "
                "(constrained decoding is for tool-less passes only)", model,
            )
        else:
            payload["format"] = response_schema
    return payload


def _parse_ollama_response(data: dict) -> str:
    message = data.get("message") or {}
    return message.get("content") or data.get("response") or ""


# ── Constrained JSON decoding (Ollama's native `format`) ────────────────────
#
# Several internal passes ask the model for a JSON object and then parse
# whatever prose comes back (src/auto_review.py is the expensive one: a whole
# GPU-seconds review thrown away because the object never closed). Ollama's
# /api/chat takes a JSON Schema in `format` and decodes under it — a state
# machine over the logits, not a plea in the prompt — so the model cannot
# emit a token that breaks the schema.
#
# Two hard limits shape everything below:
#   * `format` is an OLLAMA-NATIVE parameter. Ollama's OpenAI-compatible /v1
#     surface has no such field and drops it without a word, so sending it
#     there would buy nothing while making us believe the answer is safe.
#   * `format` + `tools` do not coexist reliably across Ollama versions, so
#     this is for tool-less passes only and never enters the agent loop.
_STRUCTURED_OUTPUT_SETTING = "local_structured_output"


def _structured_output_enabled() -> bool:
    """The `local_structured_output` setting: "auto" (default) or "off".

    "off" restores the previous behaviour exactly: no schema is attached to
    any request and every caller falls back to the tolerant text parser it
    still carries.
    """
    try:
        from src.settings import get_setting
        raw = get_setting(_STRUCTURED_OUTPUT_SETTING, "auto")
    except Exception:
        return True
    value = str("auto" if raw is None else raw).strip().lower()
    return value not in ("off", "false", "0", "no", "none", "disabled")


#: Wire field that carries a JSON Schema, per backend. Ollama's native
#: /api/chat takes `format`; llama-server takes `response_format` on its
#: OpenAI-compatible surface and compiles the schema to a GBNF grammar.
_SCHEMA_FIELD_OLLAMA = "format"
_SCHEMA_FIELD_OPENAI = "response_format"


def _schema_transport(url: str) -> Optional[str]:
    """Which wire field can carry a JSON Schema to `url`, or None.

    Two local backends can decode under a schema, and they take it in
    different fields:

    * native Ollama `/api/chat` -> ``format``
    * llama-server `/v1/chat/completions` -> ``response_format``

    Everything else (a hosted API, Ollama's own /v1 surface, an endpoint we
    cannot identify) gets None and keeps the tolerant text parser. The
    llama.cpp side is answered from the managed-engine registry with
    ``probe=False``: a request on the hot path must never pay a network probe
    to find out how to phrase itself.
    """
    if _is_ollama_native_url(url):
        return _SCHEMA_FIELD_OLLAMA
    try:
        from src.model_backend import serving_backend
        if serving_backend(url, probe=False).get("backend") == "llamacpp":
            return _SCHEMA_FIELD_OPENAI
    except Exception as exc:  # noqa: BLE001
        logger.debug("llm_core: schema transport unknown for %s: %s", url, exc)
    return None


def _resolve_response_schema(url: str, response_schema: Optional[Dict]) -> Optional[Dict]:
    """The schema to actually put on the wire for `url`, or None.

    Gated on capability exactly like `think` and `num_ctx` are: only a backend
    that decodes under a schema gets one, so a hosted API or an endpoint we
    cannot identify gets nothing and keeps today's parsing. Which field the
    schema travels in is `_schema_transport`'s business, not the caller's.
    """
    if not response_schema or not isinstance(response_schema, dict):
        return None
    if not _structured_output_enabled():
        return None
    if not _schema_transport(url):
        return None
    return response_schema


def _apply_openai_response_format(
    payload: Dict, url: str, schema: Optional[Dict], tools: Optional[List] = None,
    *, model: str = "",
) -> bool:
    """Attach `schema` to an OpenAI-shaped payload for llama-server.

    Returns whether it was attached, so a caller can log the difference rather
    than assume it. The gate lives here and not at the call sites: a schema
    that reaches a payload builder which silently ignores it is worse than no
    schema at all, because the caller then trusts JSON that nothing enforced.

    `tools` excludes the schema for the same reason as on the Ollama side --
    the grammar and the tool-call decoder compete for the same output.
    """
    if not schema or not isinstance(schema, dict):
        return False
    if tools:
        logger.debug("Not sending `response_format` alongside `tools` "
                     "(constrained decoding is for tool-less passes only)")
        return False
    if _schema_transport(url) != _SCHEMA_FIELD_OPENAI:
        return False
    payload[_SCHEMA_FIELD_OPENAI] = {
        "type": "json_schema",
        "json_schema": {"name": "response", "strict": True, "schema": schema},
    }
    _suppress_thinking(payload, model)
    return True


def is_local_backend(url: str) -> bool:
    """Is this endpoint one we host ourselves?

    Ollama on either of its surfaces, or a managed llama.cpp engine. Answered
    without a network probe, because every caller is on a hot path.
    """
    try:
        if _is_ollama_native_url(url) or _is_ollama_openai_compat_url(url):
            return True
        from src.model_backend import serving_backend
        return serving_backend(url, probe=False).get("backend") in ("ollama", "llamacpp")
    except Exception as exc:  # noqa: BLE001
        logger.debug("llm_core: could not classify %s as local: %s", url, exc)
        return False


def resolve_timeout(url: str, timeout: Optional[int]) -> int:
    """The timeout to use, giving a local backend room to think.

    A caller that named a timeout gets it unchanged. Only the default is
    replaced, and only for an endpoint we host: 30 s is a sensible ceiling for
    a hosted API and a guaranteed cut-off for a quantised 27B that reasons
    first (23.0 s for three sentences, 74.9 s for a longer answer, measured).
    """
    if timeout is not None and timeout != LLMConfig.DEFAULT_TIMEOUT:
        return int(timeout)
    if is_local_backend(url):
        return LLMConfig.LOCAL_DEFAULT_TIMEOUT
    return int(timeout if timeout is not None else LLMConfig.DEFAULT_TIMEOUT)


def _suppress_thinking_for_small_talk(payload: Dict, model: str,
                                      messages: Optional[List] = None,
                                      tools: Optional[List] = None) -> bool:
    """Skip the reasoning on a turn that plainly does not need it.

    `src/turn_effort.py` holds the rule and the reasoning behind it. The short
    version: reasoning and the answer share one token budget, and on "dime en
    dos frases qué eres" the whole budget went to the reasoning -- 37.2 s and
    nothing said, against 5.5 s and a correct answer with it off.

    Only a recognised greeting or pleasantry qualifies, and never a turn that
    carries tools. Everything else keeps what it has today.
    """
    if not _supports_thinking(model):
        return False
    # An explicit ask wins over a heuristic. `gen_overrides={"think": True}`
    # is somebody saying "think about this one", and it is applied to the
    # payload before this runs; a whitelist that then turned the reasoning
    # back off would be overruling the person who asked for it. Caught by
    # tests/test_llm_core_temperature_reasoning.py, which sends "hi" -- small
    # talk by every rule here -- with think=True on purpose.
    if (payload.get("chat_template_kwargs") or {}).get("enable_thinking") is True:
        return False
    if payload.get("think") is True or payload.get("reasoning_budget"):
        return False
    try:
        from src.turn_effort import wants_reasoning
        if wants_reasoning(messages, tools=tools):
            return False
    except Exception as exc:  # noqa: BLE001 -- never fail a turn over this
        logger.debug("turn_effort unavailable: %s", exc)
        return False
    _suppress_thinking(payload, model)
    logger.debug("Small talk: answering %s without reasoning", model)
    return True


def _drop_tools_for_small_talk(payload: Dict, messages: Optional[List] = None) -> bool:
    """Send a greeting without the toolset attached.

    Agent mode is the default here, so "hola" arrives carrying every tool
    schema the workspace has, and the engine has to read all of it before it
    can say a word. Measured on this install with "hola", alternating arms:
    with the tools 7.5 s against 2.2 s without, on a cold prompt cache -- the
    block is 25 KB of schemas, 6,356 prompt tokens. Once the engine has it
    cached the gap closes to 2.1 s against 1.7 s, so what this really saves is
    the first greeting of a session, which is the one anybody notices.

    `src/turn_effort.py` decides, with the same whitelist that governs the
    reasoning: a recognised pleasantry, nothing that smells of work, and no
    tool used anywhere in the conversation yet. Anything else keeps its tools.

    The cost is a cache miss later: the next turn that does carry tools has to
    prefill that block. It would have paid that on its first tool turn anyway,
    and it is a turn that is already long, so the trade favours the greeting
    the user is sitting and watching.
    """
    if not payload.get("tools"):
        return False
    try:
        from src.turn_effort import wants_tools
        if wants_tools(messages):
            return False
    except Exception as exc:  # noqa: BLE001 -- never fail a turn over this
        logger.debug("turn_effort unavailable: %s", exc)
        return False
    payload.pop("tools", None)
    payload.pop("tool_choice", None)
    logger.debug("Small talk: answering without the toolset attached")
    return True


def _suppress_thinking(payload: Dict, model: str) -> None:
    """Turn a thinking model's reasoning off for this request.

    Used in two places, for the same underlying reason: reasoning and the
    answer come out of one token budget, so whenever the reasoning is not what
    we are paying for, it is in the way.

    llama-server applies the grammar to the content channel only, so a model
    that reasons first spends the token budget thinking and the grammar never
    gets its turn. Measured on the local 27B asking for a seven-field object:
    1200 completion tokens, 5,542 characters of reasoning, `finish_reason` of
    "length" and an empty content string -- returned as HTTP 200, which is the
    worst shape a failure can take, because every layer above reads it as a
    healthy answer that happens to be blank.

    With the reasoning off the same request answered in 48 s with the full
    object and `finish_reason` "stop", on half the tokens. Nothing is lost: a
    constrained answer is a form to fill, and thinking out loud about how to
    fill it competes for the budget that fills it. (Asking in the prompt does
    not work -- the model reasons anyway. It has to be the template flag.)

    This mirrors what the Ollama /v1 path already does with `think: False`.
    """
    if not _supports_thinking(model):
        return
    kwargs = payload.setdefault("chat_template_kwargs", {})
    if isinstance(kwargs, dict):
        kwargs["enable_thinking"] = False


def _route_for_response_schema(url: str, model: str) -> str:
    """Move a local Ollama /v1 call to the native /api/chat so it can carry a
    schema — same server, same model, same messages, only the wire format
    changes. This is the reroute `_route_for_gen_overrides` already performs
    for `think`, with a stricter proof of identity: we only move the request
    once /api/show has actually answered for this model, because that answer
    is what tells us the thing listening on :11434 really is Ollama and not a
    llama.cpp or vLLM server wearing the same port. When it does not answer,
    the URL is left alone and the caller keeps the tolerant parse.
    """
    try:
        port = urlparse(url or "").port
    except ValueError:
        return url
    if not (port == 11434 and _is_ollama_openai_compat_url(url)):
        return url
    if _ollama_model_caps(url, model) is None:
        return url
    return _ollama_native_url_for_compat(url)


def _host_match(url: str, *domains: str) -> bool:
    """Return True if url's hostname equals any of `domains` or is a subdomain of one.

    Used by helpers that want "is this Anthropic?" / "is this OpenRouter?"
    style checks. Prefer this over substring matching on the URL: the
    substring form gives wrong answers for unrelated paths or query strings
    that happen to contain the domain text.
    """
    if not url:
        return False
    try:
        # rstrip(".") so a fully-qualified host with a trailing dot
        # ("api.anthropic.com.") still matches "anthropic.com".
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


# Kimi Code subscription keys (api.kimi.com/coding/v1) require a whitelisted
# coding-agent User-Agent; otherwise the API returns 403 access_terminated_error.
# Tried in order; first success is cached per base URL for later requests.
KIMI_CODE_USER_AGENTS: tuple[str, ...] = (
    "claude-code/0.1.0",
    "claude-code/1.0.0",
    "KimiCLI/1.0",
    "Kilo-Code/1.0",
    "Roo-Code/1.0",
    "Cursor/1.0",
)
KIMI_CODE_USER_AGENT = KIMI_CODE_USER_AGENTS[0]
_kimi_code_ua_cache: dict[str, str] = {}


def _is_kimi_code_url(url: str) -> bool:
    if not url or not _host_match(url, "kimi.com"):
        return False
    try:
        return "/coding" in (urlparse(url).path or "")
    except Exception:
        return False


def _kimi_code_base_key(url: str) -> str:
    """Normalize a Kimi Code chat/models URL to its OpenAI base (.../coding/v1)."""
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    for suffix in ("/chat/completions", "/models", "/completions"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    path = path.rstrip("/") or "/coding/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _is_kimi_code_access_denied(status: int, body: bytes | str) -> bool:
    if status != 403:
        return False
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else (body or "")
    lower = text.lower()
    return (
        "access_terminated_error" in lower
        or "coding agents" in lower
        or "only available for coding" in lower
    )


def _kimi_code_ua_candidates(url: str) -> list[str]:
    if not _is_kimi_code_url(url):
        return []
    base_key = _kimi_code_base_key(url)
    cached = _kimi_code_ua_cache.get(base_key)
    if cached:
        return [cached] + [ua for ua in KIMI_CODE_USER_AGENTS if ua != cached]
    return list(KIMI_CODE_USER_AGENTS)


def _remember_kimi_code_user_agent(url: str, user_agent: str) -> None:
    _kimi_code_ua_cache[_kimi_code_base_key(url)] = user_agent


def apply_kimi_code_headers(headers: Optional[Dict], url: str) -> Dict[str, str]:
    """Pick a Kimi Code User-Agent (cached probe when possible)."""
    h = dict(headers or {})
    if not _is_kimi_code_url(url):
        return h
    base_key = _kimi_code_base_key(url)
    cached = _kimi_code_ua_cache.get(base_key)
    if cached:
        h["User-Agent"] = cached
        return h
    models_url = base_key.rstrip("/") + "/models"
    from src.tls_overrides import llm_verify
    for ua in KIMI_CODE_USER_AGENTS:
        trial = dict(h)
        trial["User-Agent"] = ua
        try:
            r = httpx.get(models_url, headers=trial, timeout=8, verify=llm_verify())
        except Exception:
            continue
        if _is_kimi_code_access_denied(r.status_code, r.content):
            logger.debug("Kimi Code rejected User-Agent %s (403), trying next", ua)
            continue
        if r.status_code < 400:
            _remember_kimi_code_user_agent(url, ua)
            h["User-Agent"] = ua
            return h
        break
    h.setdefault("User-Agent", KIMI_CODE_USER_AGENT)
    return h


async def apply_kimi_code_headers_async(client, headers: Optional[Dict], url: str) -> Dict[str, str]:
    """Pick a Kimi Code User-Agent without blocking the event loop."""
    h = dict(headers or {})
    if not _is_kimi_code_url(url):
        return h
    base_key = _kimi_code_base_key(url)
    cached = _kimi_code_ua_cache.get(base_key)
    if cached:
        h["User-Agent"] = cached
        return h
    models_url = base_key.rstrip("/") + "/models"
    for ua in KIMI_CODE_USER_AGENTS:
        trial = dict(h)
        trial["User-Agent"] = ua
        try:
            r = await client.get(models_url, headers=trial, timeout=8)
        except Exception:
            continue
        if _is_kimi_code_access_denied(r.status_code, r.content):
            logger.debug("Kimi Code rejected User-Agent %s (403), trying next", ua)
            continue
        if r.status_code < 400:
            _remember_kimi_code_user_agent(url, ua)
            h["User-Agent"] = ua
            return h
        break
    h.setdefault("User-Agent", KIMI_CODE_USER_AGENT)
    return h


def httpx_get_kimi_aware(url: str, headers: Optional[Dict], **kwargs):
    h = apply_kimi_code_headers(headers, url)
    if not _is_kimi_code_url(url):
        return httpx.get(url, headers=h, **kwargs)
    last = None
    for ua in _kimi_code_ua_candidates(url):
        trial = dict(h)
        trial["User-Agent"] = ua
        last = httpx.get(url, headers=trial, **kwargs)
        if not _is_kimi_code_access_denied(last.status_code, last.content):
            if last.status_code < 400:
                _remember_kimi_code_user_agent(url, ua)
            return last
    return last


def httpx_post_kimi_aware(url: str, headers: Optional[Dict], **kwargs):
    h = apply_kimi_code_headers(headers, url)
    if not _is_kimi_code_url(url):
        return httpx.post(url, headers=h, **kwargs)
    last = None
    for ua in _kimi_code_ua_candidates(url):
        trial = dict(h)
        trial["User-Agent"] = ua
        last = httpx.post(url, headers=trial, **kwargs)
        if not _is_kimi_code_access_denied(last.status_code, last.content):
            if last.status_code < 400:
                _remember_kimi_code_user_agent(url, ua)
            return last
    return last


async def httpx_post_kimi_aware_async(client, url: str, headers: Optional[Dict], **kwargs):
    h = await apply_kimi_code_headers_async(client, headers, url)
    if not _is_kimi_code_url(url):
        return await client.post(url, headers=h, **kwargs)
    last = None
    for ua in _kimi_code_ua_candidates(url):
        trial = dict(h)
        trial["User-Agent"] = ua
        last = await client.post(url, headers=trial, **kwargs)
        if not _is_kimi_code_access_denied(last.status_code, last.content):
            if last.status_code < 400:
                _remember_kimi_code_user_agent(url, ua)
            return last
    return last


def _detect_provider(url: str) -> str:
    """Detect the API provider from a configured endpoint URL.

    Matches on hostname (exact or subdomain) rather than substring, so a URL
    that merely contains a provider's domain in its path or query — or a
    look-alike host such as ``anthropic.com.example`` — is not misclassified.
    Unknown hosts fall back to the OpenAI-compatible default, which the
    majority of providers implement.
    """
    if _is_ollama_native_url(url):
        return "ollama"
    if _host_match(url, "anthropic.com"):
        return "anthropic"
    if _host_match(url, "opencode.ai/zen/go"):
        return "opencode-go"
    if _host_match(url, "opencode.ai/zen"):
        return "opencode-zen"
    if _host_match(url, "openrouter.ai"):
        return "openrouter"
    if _host_match(url, "groq.com"):
        return "groq"
    if _host_match(url, "nvidia.com"):
        return "nvidia"
    if _host_match(url, "moonshot.ai") or _host_match(url, "moonshot.cn"):
        return "moonshot"
    from src.chatgpt_subscription import is_chatgpt_subscription_base
    if is_chatgpt_subscription_base(url):
        return "chatgpt-subscription"
    from src.copilot import is_copilot_base
    if is_copilot_base(url):
        return "copilot"
    if _host_match(url, "cerebras.ai"):
        return "cerebras"
    if _host_match(url, "mistral.ai"):
        return "mistral"
    return "openai"


def _is_self_hosted_openai_compatible(url: str) -> bool:
    """True for custom/local OpenAI-compatible servers (llama.cpp, LM Studio,
    vLLM, text-generation-webui, etc.) as opposed to cloud APIs.

    Used to gate llama.cpp-server-specific payload extras (``session_id``,
    ``cache_prompt``) used for KV-cache slot affinity (issue #2927). Strict
    cloud providers reject unrecognized top-level fields (api.openai.com
    returns 400, Mistral returns 422 "extra_forbidden", issue #3793), and any
    unknown OpenAI-compatible host used to be treated as self-hosted, so those
    fields leaked to every strict provider added as a custom endpoint.

    A server only counts as self-hosted when it also resolves as local:
    loopback/private/tailscale host, or the endpoint explicitly configured
    with kind "local". A self-hosted server exposed via a public hostname
    loses the affinity hint unless its endpoint kind is set to "local" -
    a lost perf hint, versus a hard 4xx on every request the other way.
    """
    if _detect_provider(url) != "openai" or _host_match(url, "openai.com"):
        return False
    from src.model_context import is_local_endpoint
    return is_local_endpoint(url)


def _apply_local_cache_affinity(payload: Dict, url: str, session_id: Optional[str]) -> None:
    """Add llama.cpp-server slot-affinity hints to an outgoing payload, in place.

    As diagnosed in issue #2927, llama.cpp assigns requests to processing
    slots via LRU when no stable identifier is present ("session_id=<empty>
    server-selected (LCP/LRU)"), which means consecutive turns of the same
    chat can land on different slots and lose their cached prefix entirely.
    Sending a stable ``session_id`` (derived from the Faustus session) lets
    the server keep routing the same conversation to the same slot, and
    ``cache_prompt: true`` asks it to retain/reuse the prefix it already has.

    Both fields are llama.cpp / LM Studio extensions to the OpenAI schema; we
    only set them for self-hosted OpenAI-compatible endpoints (never
    api.openai.com or other cloud providers, which reject unrecognized
    top-level request fields).
    """
    if not session_id:
        return
    if not _is_self_hosted_openai_compatible(url):
        return
    payload.setdefault("session_id", str(session_id))
    payload.setdefault("cache_prompt", True)


def _is_local_minimax_mlx_request(url: str, model: str) -> bool:
    """Local MLX MiniMax-family endpoints need conservative sampling defaults.

    The OpenAI-compatible MLX server accepts repetition/frequency penalties.
    Some large quantized MiniMax/MoE ports otherwise fall into visible reasoning
    loops ("Also be...", "No.", etc.) even for trivial prompts.
    """
    if not model:
        return False
    m = model.lower()
    if "minimax" not in m and "mini-max" not in m:
        return False
    try:
        from src.model_context import is_local_endpoint
        return is_local_endpoint(url)
    except Exception:
        return False


def _apply_local_generation_stability(payload: Dict, url: str, model: str) -> None:
    """Sampling/output-length safety net for a local OpenAI-compatible
    server's JSON body, applied AFTER `_apply_gen_overrides_openai` (an
    explicit saved/per-turn value always wins — everything here is
    `setdefault`).

    Two local families are covered, each gated on its own detector so one
    does not leak the other's defaults:
      * MiniMax MLX quantized ports (`_is_local_minimax_mlx_request`) — the
        original, unchanged conservative preset below (issue predates the
        generic branch).
      * Any OTHER self-hosted OpenAI-compatible endpoint
        (`_is_self_hosted_openai_compatible`: llama.cpp's llama-server,
        vLLM, LM Studio-style servers, ... — never a cloud provider, never
        Ollama, which has its own native-endpoint floor in
        `_model_load_defaults`/`_apply_gen_overrides_ollama`). Verified
        live: a llama-server chat turn with none of this arrived with
        `max_tokens=-1, repeat_penalty=1.0, min_p=0` and ran unbounded for
        15 minutes / 7800+ tokens — llama-server accepts `min_p`/
        `repeat_penalty`/`top_k` as top-level OpenAI-schema extensions (the
        same fields `_apply_gen_overrides_openai` already forwards when an
        override sets them), and `-1`/absent `max_tokens` is "no limit" the
        same way it is for Ollama. There is no per-model `extra` concept for
        a generic custom endpoint the way there is for Ollama
        (`model_load_options` is Ollama-only), so the SAME
        `local_repeat_penalty_default`/`local_min_p_default` floor settings
        apply directly here.
    """
    if _is_local_minimax_mlx_request(url, model):
        if "temperature" in payload:
            try:
                # MiniMax MLX quantized ports are very sensitive to chat/agent
                # harness size. Character presets can ask for a warmer voice, but
                # local MiniMax needs a final compatibility clamp or trivial
                # prompts can fall into visible reasoning/repetition loops.
                payload["temperature"] = min(float(payload.get("temperature") or 0.2), 0.2)
            except (TypeError, ValueError):
                payload["temperature"] = 0.2
        payload.setdefault("top_p", 0.9)
        payload.setdefault("top_k", 20)
        payload.setdefault("repetition_penalty", 1.12)
        payload.setdefault("repetition_context_size", 256)
        payload.setdefault("frequency_penalty", 0.08)
        payload.setdefault("frequency_context_size", 256)
        payload.setdefault("presence_penalty", 0.02)
        payload.setdefault("presence_context_size", 256)
        payload.setdefault("stop", ["<|im_end|>", "<|endoftext|>", "</s>"])
        # A max_tokens of 0 means "server default/unbounded" for many local
        # endpoints. Keep simple chats from running forever when the model loops.
        if not payload.get("max_tokens") and not payload.get("max_completion_tokens"):
            payload["max_tokens"] = 2048
        return

    if _is_self_hosted_openai_compatible(url) and not _is_local_ollama_target(url):
        # Real Ollama /v1 traffic never reaches this branch on the actual
        # request path (`_route_for_gen_overrides` already moves it to the
        # native `/api/chat` endpoint, which takes the `ollama` provider
        # branch instead) — the exclusion is a belt-and-braces guard against
        # double-applying the floor if this helper is ever called directly
        # or the reroute is bypassed.
        payload.setdefault("repeat_penalty", _local_sampler_default("local_repeat_penalty_default", 1.05))
        payload.setdefault("min_p", _local_sampler_default("local_min_p_default", 0.05))
        # `local_top_p_default`/`local_top_k_default`: same floor as the two
        # above (`setdefault` only — an explicit `/topp`/`/topk` already
        # landed in `payload` via `_apply_gen_overrides_openai`, which runs
        # BEFORE this function, so it always wins). The temperature floor
        # for this path is applied earlier, in `_stream_agent_loop_body`,
        # where the explicit/default distinction is tracked (`temperature`
        # already carries the right value by the time it reaches `payload`
        # here). 0/empty (`_local_sampler_default_optional` returns None)
        # means "do not send".
        if "top_p" not in payload:
            _top_p = _local_sampler_default_optional("local_top_p_default")
            if _top_p is not None:
                payload["top_p"] = _top_p
        if "top_k" not in payload:
            _top_k = _local_sampler_default_optional("local_top_k_default")
            if _top_k is not None:
                payload["top_k"] = int(_top_k)
        if not payload.get("max_tokens") and not payload.get("max_completion_tokens"):
            try:
                cap = int(_local_sampler_default("local_openai_max_tokens_default", 8192))
            except (TypeError, ValueError):
                cap = 8192
            payload["max_tokens"] = cap if cap > 0 else 8192


def _provider_headers(provider: str, headers: Optional[Dict] = None) -> Dict[str, str]:
    h = {"Content-Type": "application/json"}
    if isinstance(headers, dict):
        h.update(headers)
    if provider == "openrouter":
        h.setdefault("HTTP-Referer", "https://github.com/odysseus-dev/odysseus")
        h.setdefault("X-OpenRouter-Title", "Faustus")
    if provider == "copilot":
        # Ensure the Copilot-required headers are present even when the caller
        # didn't pass pre-built headers (e.g. model listing). build_headers()
        # already injects these for the live chat path; setdefault keeps any
        # request-specific values (x-initiator/vision) the caller set.
        from src.copilot import copilot_headers
        for k, v in copilot_headers(None).items():
            h.setdefault(k, v)
    return h


def _provider_label(url: str) -> str:
    """Human-friendly provider name for error messages."""
    if not url:
        return "provider"
    if _host_match(url, "anthropic.com"): return "Anthropic"
    if _host_match(url, "ollama.com"): return "Ollama Cloud"
    if _host_match(url, "x.ai"): return "xAI"
    if _host_match(url, "openai.com"): return "OpenAI"
    if _host_match(url, "openrouter.ai"): return "OpenRouter"
    if _host_match(url, "opencode.ai/zen/go"): return "OpenCode Go"
    if _host_match(url, "opencode.ai/zen"): return "OpenCode Zen"
    if _host_match(url, "groq.com"): return "Groq"
    from src.chatgpt_subscription import is_chatgpt_subscription_base
    if is_chatgpt_subscription_base(url): return "ChatGPT Subscription"
    from src.copilot import is_copilot_base
    if is_copilot_base(url): return "GitHub Copilot"
    if _host_match(url, "cerebras.ai"):
        return "cerebras"
    if _host_match(url, "mistral.ai"): return "Mistral"
    if _host_match(url, "deepseek.com"): return "DeepSeek"
    if _host_match(url, "nvidia.com"): return "NVIDIA"
    if _host_match(url, "googleapis.com"): return "Google"
    if _host_match(url, "together.xyz", "together.ai"): return "Together"
    if _host_match(url, "fireworks.ai"): return "Fireworks"
    if _host_match(url, "kimi.com"):
        try:
            if "/coding" in (urlparse(url).path or ""):
                return "Kimi Code"
        except Exception:
            pass
    if _is_ollama_native_url(url): return "Ollama"
    try:
        _parsed_local = urlparse(url)
        host = (_parsed_local.hostname or "").lower()
        port = _parsed_local.port
    except Exception:
        return "provider"
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        # A port alone is not authoritative: vLLM, SGLang, llama.cpp and plain
        # OpenAI-compatible servers all routinely share 8000/8080, so naming the
        # serving tool from the port here would mislabel real setups. The tool is
        # identified by probing llama-server's native /props endpoint during
        # discovery (see ModelDiscovery._fingerprint_provider); this stays neutral.
        return "local endpoint"
    return host or "provider"


def _is_openai_hosted_chat_url(url: str) -> bool:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return False
    path = (parsed.path or "").rstrip("/")
    return _host_match(url, "openai.com") and path.endswith("/chat/completions")


def _model_disallows_reasoning_effort_with_chat_tools(model: str) -> bool:
    """OpenAI GPT 5.x variants reject reasoning_effort + tools on chat completions."""
    m = (model or "").strip().lower()
    return bool(re.match(r"^(?:openai/)?gpt-5(?:[.\-]\d+)?(?:[-_:].*)?$", m))


# gpt-oss (harmony) ships BUILT-IN tools named `python` and `browser`, invoked
# with the raw body as the argument (`to=python` + bare source), while custom
# functions use `to=functions.NAME` + JSON. A tool we expose under a built-in's
# name therefore gets called with the built-in convention: the model emits raw
# code, the server tries to parse it as JSON, and the whole request dies
# ("error parsing tool call: raw='import sys, ...'"). In streaming mode Ollama
# does not even report it — it truncates the stream, so the turn looks like an
# empty response. `bash` collides the same way in practice.
#
# Measured on gpt-oss:20b via Ollama /v1 with a fixed agentic prompt:
#   tools named python+bash ............ 2/6 succeeded (4 parse failures)
#   python renamed ..................... 5/6
#   python and bash renamed ............ 6/6
#
# So rename the colliding tools on the way out and map the names back on the
# way in. Confined to the transport layer: callers keep using the real names.
_HARMONY_TOOL_ALIASES = {
    "python": "run_python_code",
    "bash": "run_shell_command",
    "browser": "web_browser_tool",
}
_HARMONY_TOOL_ALIASES_REVERSE = {v: k for k, v in _HARMONY_TOOL_ALIASES.items()}


def _is_harmony_model(model: str) -> bool:
    """True for gpt-oss / harmony-format models, which have built-in tool names."""
    return "gpt-oss" in (model or "").lower()


def _alias_harmony_tools(tools: Optional[List[Dict]], model: str) -> Optional[List[Dict]]:
    """Rename tools that collide with harmony built-ins. Returns a copy."""
    if not tools or not _is_harmony_model(model):
        return tools
    out = []
    for t in tools:
        fn = t.get("function") or {}
        alias = _HARMONY_TOOL_ALIASES.get(fn.get("name"))
        if alias:
            t = copy.deepcopy(t)
            t["function"]["name"] = alias
        out.append(t)
    return out


def _unalias_harmony_tool_name(name: str, model: str) -> str:
    """Map an aliased tool name in a model response back to the real name."""
    if not _is_harmony_model(model):
        return name
    return _HARMONY_TOOL_ALIASES_REVERSE.get(name, name)


def _scrub_openai_chat_tool_reasoning(payload: Dict, target_url: str, model: str) -> None:
    if not payload.get("tools"):
        return
    if not _is_openai_hosted_chat_url(target_url):
        return
    if not _model_disallows_reasoning_effort_with_chat_tools(model):
        return
    payload["reasoning_effort"] = "none"


def _normalize_chatgpt_subscription_url(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    if base.endswith("/responses"):
        return base
    return base + "/responses"


def _message_content_as_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                if part:
                    parts.append(str(part))
                continue
            if isinstance(part.get("text"), str):
                parts.append(part["text"])
                continue
            if isinstance(part.get("content"), str):
                parts.append(part["content"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def _chatgpt_subscription_instructions(messages: List[Dict]) -> str:
    instructions = [
        _message_content_as_text(msg.get("content")).strip()
        for msg in messages or []
        if (msg.get("role") or "") == "system"
    ]
    instructions = [part for part in instructions if part]
    if instructions:
        return "\n\n".join(instructions)
    return "You are a helpful AI assistant."


def _build_chatgpt_responses_payload(
    model: str,
    messages: List[Dict],
    temperature: float,
    max_tokens: int,
    *,
    stream: bool = False,
) -> Dict:
    from src.chatgpt_subscription import build_responses_input

    conversation = [msg for msg in (messages or []) if (msg.get("role") or "") != "system"]
    payload: Dict = {
        "model": model,
        "instructions": _chatgpt_subscription_instructions(messages),
        "input": build_responses_input(conversation),
        "stream": stream,
        "store": False,
    }
    if not _restricts_temperature(model):
        payload["temperature"] = temperature
    # ChatGPT Subscription Codex API does not support max_output_tokens —
    # passing it returns HTTP 400 "Unsupported parameter: max_output_tokens".
    # Do not include it in the payload.
    return payload


def _format_chatgpt_subscription_error(status_code: int, text: str) -> str:
    if status_code in (401, 403):
        return "ChatGPT Subscription credentials expired or were rejected. Reconnect the provider."
    if status_code == 429:
        return "ChatGPT Subscription quota or rate limit was reached. Retry after the upstream limit resets."
    return _format_upstream_error(status_code, text, "https://chatgpt.com/backend-api/codex")


def _format_upstream_error(status: int, body: bytes | str, url: str) -> str:
    """Turn an upstream HTTP error into a user-readable sentence.

    Auth failures (401/403) become 'xAI rejected the API key' etc., so the UI
    stops showing raw JSON like '{"error":{"message":"User not found."}}'.
    """
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="replace")
        except Exception:
            body = str(body)
    provider = _provider_label(url)
    # Try to pull a message out of the body
    detail = ""
    try:
        j = json.loads(body) if body else {}
        if isinstance(j, dict):
            err = j.get("error") or j
            if isinstance(err, dict):
                detail = (err.get("message") or err.get("detail") or "").strip()
            elif isinstance(err, str):
                detail = err.strip()
    except Exception:
        detail = (body or "").strip()[:240]

    if status in (401, 403):
        msg = f"{provider} rejected the API key"
        if status == 403:
            msg = f"{provider} denied access (403)"
        if detail:
            msg += f" — {detail}"
        msg += ". Check Model Endpoints → {} and re-paste the key.".format(provider)
        return msg
    if status == 404:
        return f"{provider} returned 404 — check the base URL and model name." + (f" ({detail})" if detail else "")
    if status == 429:
        return f"{provider} rate-limited the request (429)." + (f" {detail}" if detail else "")
    if status >= 500:
        return f"{provider} is having an outage (HTTP {status})." + (f" {detail}" if detail else "")
    return f"{provider} returned HTTP {status}" + (f": {detail}" if detail else "")

# Models that require max_completion_tokens instead of max_tokens
_MAX_COMPLETION_TOKENS_MODELS = {"o1", "o3", "o4", "gpt-4.5", "gpt-5"}

def _uses_max_completion_tokens(model: str) -> bool:
    """Check if a model requires max_completion_tokens instead of max_tokens."""
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) or f"/{p}" in m for p in _MAX_COMPLETION_TOKENS_MODELS)

# OpenAI reasoning models (o1, o3, o4, gpt-5 families) only accept the default
# temperature. Sending any explicit value — even 0.0 — returns HTTP 400
# ("Only the default (1) value is supported"). That otherwise breaks chat when a
# preset sets a non-default temperature, and makes endpoint probing report a
# perfectly good model as failing. For these models we omit the field and let
# the API use its required default. (gpt-4.5 is intentionally excluded — it is
# not a reasoning model and accepts temperature normally.)
_FIXED_TEMPERATURE_MODELS = ("o1", "o3", "o4", "gpt-5", "kimi-for-coding")

def _restricts_temperature(model: str) -> bool:
    """Check if a model rejects any non-default temperature."""
    if not model:
        return False
    m = model.lower()
    return any(m.startswith(p) or f"/{p}" in m for p in _FIXED_TEMPERATURE_MODELS)


# The official Moonshot API fixes temperature at 1.0 in thinking mode and 0.6
# when thinking is explicitly disabled for Kimi K2.5/K2.6. Any other explicit
# value returns HTTP 400. Faustus does not currently send the `thinking` mode
# control, so omit temperature and let Moonshot use its default thinking mode.
# Keep the gate provider-specific: self-hosted Kimi deployments may accept
# custom sampling values, and older Moonshot models have different defaults.
def _moonshot_rejects_custom_temperature(provider: str, model: str) -> bool:
    """Check if the official Moonshot API fixes temperature for this model."""
    if provider != "moonshot" or not isinstance(model, str):
        return False
    model_id = model.lower().rsplit("/", 1)[-1]
    return bool(re.match(r"^kimi-k2\.(?:5|6)(?:$|[-_:])", model_id))


def _omit_temperature(provider: str, model: str) -> bool:
    """Check if a request should use the provider's default temperature."""
    return _restricts_temperature(model) or _moonshot_rejects_custom_temperature(
        provider, model
    )


# Anthropic removed the sampling parameters (temperature, top_p, top_k) starting
# with Claude Opus 4.7. On Opus 4.7 and later, sending `temperature` at all —
# even 0.0 — returns HTTP 400. Earlier Claude models (Opus 4.6 and below, every
# Sonnet/Haiku) still accept temperature in [0.0, 1.0], so the omission must be
# version-gated rather than applied to all `claude-*` models.
def _anthropic_rejects_temperature(model: str) -> bool:
    """Check if a native-Anthropic model rejects the temperature field (Opus 4.7+)."""
    if not isinstance(model, str) or not model:
        return False
    # `(?<![a-z])` anchors "opus" to a word boundary so a substring match like
    # `oct-opus`/`octopus-4-8` can't be read as Opus (it would otherwise strip
    # temperature). Both version components are capped at 1-2 digits and forbid a
    # trailing digit, so an 8-digit date can never be read as a version number:
    # `claude-opus-4-20250514` (Opus 4.0) parses as major-only rather than reading
    # `20250514` as a giant minor, and `claude-3-opus-20240229` (legacy Claude 3
    # Opus, date directly after "opus-") fails to match at all rather than reading
    # the date as a giant major. Dated 4.7+ snapshots (`claude-opus-4-7-20260201`)
    # keep their explicit minor and are still matched.
    #
    # The minor is optional and a missing minor reads as `.0`, so major-only ids
    # like `claude-opus-5` are correctly treated as >= 4.7 (issue #5753). Without
    # this, every Opus 5 call kept `temperature` and failed with HTTP 400 — visible
    # only on paths that pass a temperature, e.g. scheduled tasks inheriting
    # `stream_agent_loop`'s 0.3 default, which returned empty responses.
    match = re.search(
        r"(?<![a-z])opus[-_]?(\d{1,2})(?!\d)(?:[-_.](\d{1,2})(?!\d))?", model.lower()
    )
    if not match:
        return False
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    return (major, minor) >= (4, 7)

# Reasoning effort level sent to Mistral thinking-capable models. Mistral's
# API accepts "high", "medium", "low", "none" — see
# https://docs.mistral.ai/capabilities/reasoning/. Override via env var
# ODYSSEUS_MISTRAL_REASONING_EFFORT (e.g. set to "medium" for cheaper chat).
_MISTRAL_REASONING_EFFORT = os.getenv("ODYSSEUS_MISTRAL_REASONING_EFFORT", "high")

# Models that support structured thinking — may output </think> without opening tag
_THINKING_MODEL_PATTERNS = (
    "qwen3", "qwq", "deepseek-r1", "deepseek-reasoner", "deepseek-v4",
    "minimax", "m2-reap", "gemma", "stepfun", "step-3", "step3",
    "magistral", "mistral-small", "mistral-medium",
)

def _supports_thinking(model: str) -> bool:
    """Check if model supports structured thinking output."""
    if not model:
        return False
    m = model.lower()
    return any(p in m for p in _THINKING_MODEL_PATTERNS)


def _resolve_think_decision(model: str, overrides: Optional[Dict]) -> Optional[bool]:
    """The same explicit-override-else-default-suppress decision the Ollama
    branches already make (an explicit `/think on|off`, the harness'
    runaway-thinking retry, or any other caller that set `think` in
    `gen_overrides` always wins; otherwise a thinking-capable model defaults
    to thinking OFF, so tool calls aren't swallowed inside `<think>` blocks
    and a chat template that opens every turn with `<think>` by default
    doesn't burn the whole output cap reasoning).

    Returns `True`/`False` when a decision applies, `None` when nothing
    about thinking should be sent at all (model has no thinking mode and
    nothing pinned one).
    """
    ov = overrides if isinstance(overrides, dict) else {}
    if "think" in ov and ov["think"] is not None:
        return bool(ov["think"])
    if _supports_thinking(model):
        return False
    return None

def _normalize_mistral_content(content):
    """Mistral returns content as a structured array when reasoning is on:
        [{"type": "thinking", "thinking": [{"type": "text", "text": "..."}], "closed": true},
         {"type": "text", "text": "...final answer..."}]
    Convert to (text, thinking) tuple of plain strings. Pass through strings
    unchanged so non-Mistral OpenAI-compat endpoints are unaffected.
    """
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return "", ""
    text_parts = []
    thinking_parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            t = block.get("text", "")
            if t:
                text_parts.append(t)
        elif btype == "thinking":
            inner = block.get("thinking", [])
            if isinstance(inner, list):
                for tb in inner:
                    if isinstance(tb, dict) and tb.get("text"):
                        thinking_parts.append(tb["text"])
            elif isinstance(inner, str):
                thinking_parts.append(inner)
    return "".join(text_parts), "".join(thinking_parts)


def _convert_openai_content_to_anthropic(content):
    """Convert OpenAI multimodal content blocks to Anthropic format.

    Converts image_url blocks (data URI) → Anthropic image blocks.
    Passes text blocks through unchanged.
    """
    if not isinstance(content, list):
        return content
    converted = []
    for block in content:
        if not isinstance(block, dict):
            converted.append(block)
            continue
        if block.get("type") == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            # Parse data URI: data:image/<fmt>;base64,<data>
            if url.startswith("data:"):
                try:
                    header, b64_data = url.split(",", 1)
                    media_type = header.split(";")[0].replace("data:", "")
                except (ValueError, IndexError):
                    continue
                converted.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64_data,
                    },
                })
            else:
                # External URL — use Anthropic's URL source
                converted.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        elif block.get("type") == "text":
            converted.append(block)
        else:
            converted.append(block)
    return converted


def _build_anthropic_payload(model, messages, temperature, max_tokens, stream=False, tools=None):
    """Convert OpenAI-style messages to Anthropic format."""
    system_parts = []
    chat_messages = []
    for m in messages:
        if m.get("role") == "system":
            system_parts.append(m.get("content") or "")
        elif m.get("role") == "tool":
            # Convert OpenAI tool result to Anthropic format
            chat_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }],
            })
        elif m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
            # Convert OpenAI assistant tool_calls to Anthropic format
            content = []
            if m.get("content"):
                content.append({"type": "text", "text": m["content"]})
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args_str = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except (json.JSONDecodeError, TypeError):
                    args = {}
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            chat_messages.append({"role": "assistant", "content": content})
        else:
            # Convert multimodal content (image_url → image) for Anthropic
            content = _convert_openai_content_to_anthropic(m["content"])
            chat_messages.append({"role": m["role"], "content": content})
    # Anthropic only accepts temperature in [0.0, 1.0] and 400s on anything above
    # 1.0. Clamp here (in the Anthropic builder only) so presets/sliders that use
    # the wider OpenAI 0.0-2.0 range — e.g. the shipped "Nietzsche" preset at 1.2
    # — don't hard-break every Claude request. OpenAI's own path is left untouched.
    if temperature is not None:
        temperature = max(0.0, min(temperature, 1.0))
    payload = {
        "model": model,
        "messages": chat_messages,
        "max_tokens": max_tokens if max_tokens and max_tokens > 0 else 4096,
    }
    # Opus 4.7+ removed the sampling parameters — sending `temperature` (even 0.0)
    # returns HTTP 400. Omit it for those models; older Claude models still take it.
    if not _anthropic_rejects_temperature(model):
        payload["temperature"] = temperature
    if system_parts:
        system_text = "\n\n".join(system_parts)
        # Send `system` as a structured text block so we can attach a prompt-cache
        # breakpoint. The agent loop re-sends this same large prefix every round;
        # caching it makes Anthropic re-read it from cache (~90% cheaper, lower TTFB)
        # instead of re-billing it. Skip caching tiny one-off prompts, where the
        # cache-WRITE premium wouldn't pay back (no reuse). Presence of `tools`
        # means an agentic/multi-round call, where the prefix is always reused.
        system_block = {"type": "text", "text": system_text}
        if _anthropic_cache_breakpoint_applies(tools, system_text):
            system_block["cache_control"] = {"type": "ephemeral"}
        payload["system"] = [system_block]
    if stream:
        payload["stream"] = True
    # Convert OpenAI-format tools to Anthropic format
    if tools:
        anthropic_tools = []
        for t in tools:
            if t.get("type") == "function":
                fn = t["function"]
                anthropic_tools.append({
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
        if anthropic_tools:
            # Cache the tool schemas too — they're stable for the whole agent run.
            # The breakpoint caches all tool defs preceding it in the request.
            anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = anthropic_tools
    return payload


def _anthropic_cache_breakpoint_applies(tools, system_text: str) -> bool:
    """Shared threshold for attaching an Anthropic prompt-cache breakpoint:
    an agentic call (tools present) always reuses its prefix every round, and
    a big enough one-off system prompt earns back the cache-WRITE premium on
    its own. `_build_anthropic_payload` above (the native Anthropic path) and
    `_apply_openrouter_anthropic_cache_hints` below (OpenRouter's
    OpenAI-compatible path, for `anthropic/*` models only) both call this, so
    an OpenRouter call gets the identical breakpoint decision a native
    Anthropic call would (OBJ-8 Lote A2).
    """
    return bool(tools) or len(system_text or "") > 4000


def _openrouter_anthropic_cache_hints_applicable(provider: str, model: str) -> bool:
    """True only for an OpenRouter call to an `anthropic/*` model. OpenRouter
    forwards a `cache_control` marker on a message content BLOCK straight
    through to Anthropic for these models — the same breakpoint mechanism
    `_build_anthropic_payload` uses on the native path — but no other
    OpenRouter model has any such mechanism, so this stays a narrow
    allow-list rather than "any openrouter call" (OBJ-8 Lote A2).
    """
    return provider == "openrouter" and str(model or "").startswith("anthropic/")


def _apply_openrouter_anthropic_cache_hints(payload: Dict, *, tools: Optional[List[Dict]] = None) -> None:
    """Mirror `_build_anthropic_payload`'s system-prompt cache breakpoint for
    an OpenRouter `anthropic/*` call riding the OpenAI-compatible payload
    shape. Callers gate this with `_openrouter_anthropic_cache_hints_applicable`
    first — it does not check `provider`/`model` itself.

    A plain string `content` has no block to attach `cache_control` to, so
    the already-consolidated system message (see the `sys_parts`/`non_sys`
    merge each `llm_core` call site does before building its payload) is
    rewritten as a one-block array — the same shape Anthropic's native
    Messages API expects — only when `_anthropic_cache_breakpoint_applies`
    says it is worth it.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    first = messages[0]
    if not isinstance(first, dict) or first.get("role") != "system":
        return
    system_text = first.get("content")
    if not isinstance(system_text, str) or not system_text:
        return
    if not _anthropic_cache_breakpoint_applies(tools, system_text):
        return
    first["content"] = [{
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral"},
    }]


def _build_anthropic_headers(headers):
    """Convert Bearer auth to x-api-key for Anthropic."""
    h = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if headers:
        for k, v in headers.items():
            if k.lower() == "authorization" and isinstance(v, str) and v.startswith("Bearer "):
                h["x-api-key"] = v[7:]
            else:
                h[k] = v
    return h

def _parse_anthropic_response(data: dict) -> str:
    """Extract text from an Anthropic response.

    The Messages API `content` is an array that can hold more than one text
    block (e.g. text split around a tool_use block, or citation-segmented
    text). Concatenate them all instead of returning only the first, which
    silently dropped the rest of the reply.
    """
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _as_content_blocks(content) -> List[Dict]:
    """Coerce a message `content` into a list of content blocks.

    A list (multimodal: text + image parts) passes through; a non-empty string
    becomes a single text block; None/empty yields no blocks. Used when merging
    consecutive user messages so multimodal content isn't str()-ed away.
    """
    if isinstance(content, list):
        return content
    if content:
        return [{"type": "text", "text": str(content)}]
    return []


def _is_untrusted_context_content(content) -> bool:
    if isinstance(content, str):
        return (
            content.startswith("UNTRUSTED SOURCE DATA\n")
            or "<<<UNTRUSTED_SOURCE_DATA>>>" in content
        )
    if isinstance(content, list):
        return any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and _is_untrusted_context_content(block.get("text") or "")
            for block in content
        )
    return False


# Synthetic assistant separator inserted between an untrusted-context user
# message and the real user turn so providers that require strict role
# alternation do not merge the two. Must NOT look like a user-visible answer:
# Qwen 3.8 (and similar) repeatedly echoed the old prose
# "Reference context received." as the entire completion (live Faustus chat
# ea43ef6c…, Silhouettes zip turns). Keep the legacy string for detection only.
_REFERENCE_CONTEXT_BOUNDARY = "<<faustus_ctx_ack>>"
_LEGACY_REFERENCE_CONTEXT_BOUNDARY = "Reference context received."
_REFERENCE_CONTEXT_BOUNDARY_ALIASES = (
    _REFERENCE_CONTEXT_BOUNDARY,
    _LEGACY_REFERENCE_CONTEXT_BOUNDARY,
)


def _message_text_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def is_reference_context_echo(text: str) -> bool:
    """True when model output is only the synthetic/legacy context boundary.

    Used by the agent harness so a parroted ack is treated as an empty round
    instead of a finished answer.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return False
    return stripped in _REFERENCE_CONTEXT_BOUNDARY_ALIASES


def strip_reference_context_echo(text: str) -> str:
    """Remove a leading/sole boundary echo so it is not saved or shown as prose."""
    raw = str(text or "")
    stripped = raw.strip()
    if not stripped:
        return raw
    for boundary in _REFERENCE_CONTEXT_BOUNDARY_ALIASES:
        if stripped == boundary:
            return ""
        if stripped.startswith(boundary):
            rest = stripped[len(boundary):].lstrip("\r\n")
            # Keep a single leading newline collapse; preserve remaining body.
            return rest
    return raw


# Providers whose API rejects two consecutive same-role messages outright
# (Anthropic's Messages API). Everywhere else — OpenAI-compatible endpoints,
# Ollama's native API, local/self-hosted servers — consecutive user messages
# are accepted, so there is no protocol reason to separate them with a
# synthetic assistant message a small local model can (and does) parrot back
# as its entire answer.
_STRICT_ALTERNATION_PROVIDERS = {"anthropic"}


def _merge_untrusted_and_user(last: Dict, item: Dict) -> Optional[Dict]:
    """Fold an untrusted-context user message and the following real user
    turn into a single user message.

    This is the preferred fix for role alternation: it satisfies "no two
    consecutive user messages" for every provider (Anthropic included)
    without inventing an assistant turn that never happened, which is what a
    local model on Ollama was parroting verbatim as its whole answer
    (``<<faustus_ctx_ack>>`` as 100% of the completion, sometimes twice in a
    row). The untrusted wrapper (header/guard markers) stays first, followed
    by a clear separator and the user's own text, so the model still reads
    the untrusted block as data rather than as something to answer to.

    Returns None when the content shapes cannot be merged this way (e.g.
    multimodal block lists on either side) so the caller can fall back.
    """
    lc = last.get("content")
    ic = item.get("content")
    if isinstance(lc, list) or isinstance(ic, list):
        return None
    last_str = str(lc) if lc is not None else ""
    item_str = str(ic) if ic is not None else ""
    if not last_str and not item_str:
        return None
    merged = dict(last)
    if item_str:
        merged["content"] = f"{last_str}\n\n--- Your message ---\n\n{item_str}"
    else:
        merged["content"] = last_str
    return merged


def _sanitize_llm_messages(messages: List[Dict], provider: Optional[str] = None) -> List[Dict]:
    """Strip Faustus-only metadata before sending messages to providers.

    Per the OpenAI chat format: user/system messages must have content; a tool
    message needs content + tool_call_id; an assistant message may carry content,
    tool_calls, or both. The old guard required content on every message, which
    dropped a valid assistant message that has only tool_calls — e.g. the
    follow-up message _append_tool_results builds for a no-prose native tool call
    (content=None, since Gemini/Ollama reject tool_calls alongside ""). Dropping
    it leaves the tool result dangling and breaks the next round.
    """
    allowed = {"role", "content", "name", "tool_call_id", "tool_calls", "function_call", "reasoning_content"}
    cleaned = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        item = {k: v for k, v in msg.items() if k in allowed and v is not None}
        role = item.get("role")
        if not role:
            continue
        if role == "assistant":
            # Rewrite poisoned history: prior turns that saved the legacy
            # boundary prose as the whole assistant answer would otherwise
            # teach the next completion to emit it again.
            content = item.get("content")
            text = _message_text_content(content).strip()
            if text in _REFERENCE_CONTEXT_BOUNDARY_ALIASES and not item.get("tool_calls"):
                item = dict(item)
                item["content"] = _REFERENCE_CONTEXT_BOUNDARY
            # Re-add an explicit content=None when the message is tool-calls-only
            # (the None was stripped above) so the provider gets the spec-correct
            # `content: null`, not an omitted key.
            if "content" not in item and item.get("tool_calls"):
                item["content"] = None
            if "content" in item or item.get("tool_calls"):
                cleaned.append(item)
        elif role == "tool":
            if "content" in item and "tool_call_id" in item:
                cleaned.append(item)
        elif "content" in item:
            cleaned.append(item)

    # Repair tool-call adjacency before sending to any OpenAI-compatible
    # provider. Trimming/compaction/retries can leave `role:"tool"` messages
    # without their immediately-preceding assistant `tool_calls` parent, which
    # DeepSeek rejects with:
    # "Messages with role 'tool' must be a response to a preceding message with
    # 'tool_calls'". Also strip unanswered assistant tool_calls; some providers
    # reject those as incomplete conversations.
    repaired: List[Dict] = []
    i = 0
    while i < len(cleaned):
        msg = cleaned[i]
        role = msg.get("role")

        if role == "tool":
            # Orphan tool result. There is no valid assistant tool_calls parent
            # immediately before this batch, so it cannot be sent.
            logger.debug("Dropping orphan tool message before provider request")
            i += 1
            continue

        tool_calls = msg.get("tool_calls") if role == "assistant" else None
        if not tool_calls:
            repaired.append(msg)
            i += 1
            continue

        call_ids = [
            str(tc.get("id"))
            for tc in tool_calls
            if isinstance(tc, dict) and tc.get("id")
        ]
        expected = set(call_ids)
        answered_ids = []
        tool_batch = []
        j = i + 1
        while j < len(cleaned) and cleaned[j].get("role") == "tool":
            tid = str(cleaned[j].get("tool_call_id") or "")
            if tid in expected and tid not in answered_ids:
                answered_ids.append(tid)
                tool_batch.append(cleaned[j])
            else:
                logger.debug("Dropping unmatched/duplicate tool message before provider request")
            j += 1

        if not tool_batch:
            plain = {k: v for k, v in msg.items() if k != "tool_calls"}
            if (plain.get("content") or "").strip():
                repaired.append(plain)
            else:
                logger.debug("Dropping unanswered assistant tool_calls before provider request")
            i = j
            continue

        answered = set(answered_ids)
        pruned_calls = [
            tc for tc in tool_calls
            if isinstance(tc, dict) and str(tc.get("id")) in answered
        ]
        fixed = dict(msg)
        fixed["tool_calls"] = pruned_calls
        if "content" not in fixed:
            fixed["content"] = None
        repaired.append(fixed)
        repaired.extend(tool_batch)
        if len(pruned_calls) != len(tool_calls):
            logger.debug("Pruned unanswered assistant tool_calls before provider request")
        i = j

    # Merge consecutive user messages to satisfy strict role alternation
    # requirements after invalid tool-call fragments have been removed.
    merged: List[Dict] = []
    for item in repaired:
        if not merged:
            merged.append(item)
            continue

        last = merged[-1]
        if last.get("role") == "user" and item.get("role") == "user":
            if _is_untrusted_context_content(last.get("content")):
                merged_msg = _merge_untrusted_and_user(last, item)
                if merged_msg is not None:
                    merged[-1] = merged_msg
                    continue
                # Content shape couldn't be merged (e.g. multimodal blocks).
                # Only strict-alternation providers (Anthropic) require a
                # separator message here; everyone else can simply carry two
                # consecutive user messages.
                if provider in _STRICT_ALTERNATION_PROVIDERS:
                    merged.append({"role": "assistant", "content": _REFERENCE_CONTEXT_BOUNDARY})
                merged.append(item)
                continue
            last_copy = dict(last)
            lc = last_copy.get("content")
            ic = item.get("content")
            if isinstance(lc, list) or isinstance(ic, list):
                # Preserve multimodal content blocks (e.g. an image part) by
                # concatenating the block lists. str()-ing a list turned an
                # image message into its Python repr and dropped the image.
                merged_blocks = _as_content_blocks(lc) + _as_content_blocks(ic)
                if merged_blocks:
                    last_copy["content"] = merged_blocks
                else:
                    last_copy.pop("content", None)
            else:
                last_str = str(lc) if lc is not None else ""
                item_str = str(ic) if ic is not None else ""
                new_content = "\n\n".join(part for part in (last_str, item_str) if part)
                if new_content:
                    last_copy["content"] = new_content
                else:
                    last_copy.pop("content", None)
            merged[-1] = last_copy
        else:
            merged.append(item)

    return merged


def _normalize_anthropic_url(url: str) -> str:
    """Ensure Anthropic URL points to /v1/messages."""
    url = url.rstrip("/")
    if url.endswith("/v1/messages"):
        return url
    if url.endswith("/v1"):
        return url + "/messages"
    return url + "/v1/messages"


def _model_list_base(url: str) -> str:
    """Normalize model/chat URLs to the configured endpoint base."""
    base = (url or "").strip().rstrip("/")
    for suffix in ("/models", "/chat/completions", "/completions", "/v1/messages", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    for suffix in ("/chat", "/tags", "/generate"):
        if base.endswith("/api" + suffix):
            base = base[: -len(suffix)].rstrip("/")
    return base


def _parse_model_cache(raw) -> List[str]:
    if not raw:
        return []
    try:
        models = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    if not isinstance(models, list):
        return []
    out = []
    seen = set()
    for item in models:
        mid = str(item or "").strip()
        if not mid or mid in seen:
            continue
        out.append(mid)
        seen.add(mid)
    return out


def _configured_cached_model_ids(
    endpoint_url: str,
    *,
    owner: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> List[str]:
    """Return cached models for a configured endpoint matching endpoint_url."""
    target = _model_list_base(endpoint_url)
    if not target:
        return []
    try:
        from src.database import SessionLocal, ModelEndpoint
    except Exception:
        return []
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)
        if endpoint_id:
            q = q.filter(ModelEndpoint.id == endpoint_id)
        if owner:
            from src.auth_helpers import owner_filter
            q = owner_filter(q, ModelEndpoint, owner)
        rows = q.all()
        for ep in rows:
            if _model_list_base(getattr(ep, "base_url", "")) != target:
                continue
            models = _parse_model_cache(getattr(ep, "cached_models", None) or getattr(ep, "models", None))
            if not models:
                continue
            hidden = set(_parse_model_cache(getattr(ep, "hidden_models", None)))
            return [m for m in models if m not in hidden]
    except Exception:
        return []
    finally:
        try:
            db.close()
        except Exception:
            pass
    return []


def _endpoint_id_for_url(url: str) -> Optional[str]:
    """The saved `ModelEndpoint.id` whose `base_url` matches `url`, or
    `None` when nothing configured lines up.

    `llm_call`/`_llm_call_async_impl`/`_stream_llm_inner` only ever receive
    a bare `url`/`model` pair, not a saved endpoint config -- but a saved
    OpenRouter endpoint's `base_url` IS that same `url` (same normalization
    `_configured_cached_model_ids` already relies on), so this recovers the
    real `endpoint_id` for `apply_openrouter_payload` instead of always
    passing `None` (which silently drops every per-endpoint preference).
    Best-effort: any DB hiccup yields `None`, same as "no match".
    """
    target = _model_list_base(url)
    if not target:
        return None
    try:
        from src.database import SessionLocal, ModelEndpoint
    except Exception:
        return None
    db = SessionLocal()
    try:
        rows = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True).all()
        for ep in rows:
            if _model_list_base(getattr(ep, "base_url", "")) == target:
                ep_id = getattr(ep, "id", None)
                return str(ep_id) if ep_id else None
    except Exception:
        return None
    finally:
        try:
            db.close()
        except Exception:
            pass
    return None


def list_model_ids(
    base_chat_url: str,
    timeout: int = LLMConfig.DEFAULT_TIMEOUT,
    headers: Optional[Dict] = None,
    *,
    owner: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> List[str]:
    """List available model IDs from an endpoint."""
    cached = _configured_cached_model_ids(base_chat_url, owner=owner, endpoint_id=endpoint_id)
    if cached:
        return cached
    provider = _detect_provider(base_chat_url)
    if provider == "anthropic":
        return list(ANTHROPIC_MODELS)
    try:
        h = {}
        if headers:
            h.update(headers)
        if provider == "ollama":
            models_url = _ollama_api_root(base_chat_url) + "/tags"
        else:
            from src.endpoint_resolver import build_models_url

            models_url = build_models_url(base_chat_url)
        r = httpx_get_kimi_aware(models_url, h, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        # Some OpenAI-compatible APIs (e.g. Together) return a bare list here.
        items = data if isinstance(data, list) else (data.get("data") or [])
        model_ids = [m.get("id") for m in items if isinstance(m, dict) and m.get("id")]
        if not model_ids and isinstance(data, dict):
            model_ids = [
                m.get("name") or m.get("model")
                for m in (data.get("models") or [])
                if m.get("name") or m.get("model")
            ]
        return model_ids
    except Exception:
        try:
            if ":11434" in base_chat_url or "ollama" in base_chat_url.lower():
                root = base_chat_url.replace("/v1/chat/completions", "").replace("/chat/completions", "").rstrip("/")
                r = httpx.get(root + "/api/tags", timeout=timeout)
                r.raise_for_status()
                return [m.get("name") or m.get("model") for m in (r.json().get("models") or []) if m.get("name") or m.get("model")]
        except Exception as e:
            logger.warning("Failed to fetch model list from configured endpoint", exc_info=e)
        return []

def normalize_model_id(
    endpoint_url: str,
    requested: str,
    timeout: int = LLMConfig.DEFAULT_TIMEOUT,
    *,
    owner: Optional[str] = None,
    endpoint_id: Optional[str] = None,
) -> Optional[str]:
    """Normalize a model ID to match available models."""
    avail = list_model_ids(endpoint_url, timeout, owner=owner, endpoint_id=endpoint_id)
    if not avail:
        return None
    if requested in avail:
        return requested
    import os as _os
    req_base = _os.path.basename(requested.rstrip("/"))
    for a in avail:
        if _os.path.basename(a.rstrip("/")) == req_base:
            return a
    return None

def llm_call(url: str, model: str, messages: List[Dict], temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
             max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS, headers: Optional[Dict] = None,
             timeout: int = LLMConfig.DEFAULT_TIMEOUT, prompt_type: Optional[str] = None,
             response_schema: Optional[Dict] = None, num_ctx: Optional[int] = None) -> str:
    """Synchronous LLM call with optional prompt type enhancement.

    ``num_ctx`` (native Ollama only) sizes the context window for this
    call instead of the server's default. Helper calls that need a few
    thousand tokens (a vision caption) pass a small one so loading the
    helper does not reserve a KV cache for the chat model's window.

    ``response_schema`` is a JSON Schema the answer must obey. It only has
    teeth on a native Ollama endpoint (``format`` on /api/chat); everywhere
    else it is dropped and the caller's own parsing still applies. Unlike
    ``llm_call_async`` this sync path does not reroute a local Ollama /v1 to
    the native endpoint to make a schema fit: no sync caller asks for one, so
    that would be an unexercised routing change on a shared code path.
    """
    if str(url or '').startswith('faustus-cli://'):
        from src.cli_model import complete
        return complete(url, model, messages, headers, timeout)
    h = _provider_headers(_detect_provider(url))
    # Tolerate headers that arrive as a JSON string (some sessions stored them
    # double-encoded) — otherwise h.update() throws "dictionary update sequence
    # element #0 has length 1; 2 is required".
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:
            headers = None
    if isinstance(headers, dict):
        h.update(headers)

    messages_copy = _sanitize_llm_messages(messages, provider=_detect_provider(url))

    # Consolidate multiple system messages into one at the start.
    sys_parts = []
    non_sys = []
    for m in messages_copy:
        if m.get("role") == "system":
            sys_parts.append(m.get('content') or '')
        else:
            non_sys.append(m)
    if sys_parts:
        messages_copy = [{"role": "system", "content": "\n\n".join(sys_parts)}] + non_sys
    else:
        messages_copy = non_sys

    provider = _detect_provider(url)
    schema = _resolve_response_schema(url, response_schema)
    cache_key = _get_cache_key(
        url, model, messages_copy, temperature, max_tokens, headers=headers,
        response_schema=schema,
    )
    cached_response = _get_cached_response(cache_key)
    if cached_response:
        logger.debug(f"Returning cached response for key: {cache_key}")
        return cached_response

    if provider == "anthropic":
        target_url = _normalize_anthropic_url(url)
        h = _build_anthropic_headers(headers)
        payload = _build_anthropic_payload(model, messages_copy, temperature, max_tokens)
    elif provider == "ollama":
        target_url = _normalize_ollama_url(url)
        payload = _build_ollama_payload(
            model, messages_copy, temperature, max_tokens,
            stream=False, num_ctx=get_context_length(url, model),
            response_schema=schema,
        )
        # Saved per-model load defaults (no reroute on this sync path: they
        # only apply when the endpoint is already the native one).
        _model_defaults = _clean_gen_overrides(_model_load_defaults(url, model))
        if _model_defaults:
            _apply_gen_overrides_ollama(payload, _model_defaults)
        if num_ctx and int(num_ctx) > 0:
            payload.setdefault("options", {})["num_ctx"] = int(num_ctx)
    else:
        target_url = _normalize_openai_chat_url(url)
        if provider == "copilot":
            from src.copilot import apply_request_headers
            apply_request_headers(h, messages_copy)
        payload = {
            "model": model,
            "messages": messages_copy,
            "temperature": temperature,
        }
        if _omit_temperature(provider, model):
            payload.pop("temperature", None)
        if max_tokens and max_tokens > 0:
            tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
            payload[tok_key] = max_tokens
        _apply_local_generation_stability(payload, target_url, model)
        # `url`, not `target_url`: the gate in `_resolve_response_schema` was
        # decided on the endpoint as configured, and the backend registry is
        # keyed the same way. Asking about the normalised /chat/completions
        # form answers "unknown" and drops the schema without a word.
        _apply_openai_response_format(payload, url, schema, model=model)
        # Same default as the async helper path: a llama-server/vLLM thinking
        # model answers a helper call without reasoning unless asked to.
        if (_supports_thinking(model) and _is_self_hosted_openai_compatible(url)
                and not _is_local_ollama_target(url)):
            _ctk = payload.get("chat_template_kwargs")
            if not isinstance(_ctk, dict):
                _ctk = {}
                payload["chat_template_kwargs"] = _ctk
            _ctk.setdefault("enable_thinking", False)
        _suppress_thinking_for_small_talk(payload, model, messages_copy)
        if provider == "mistral" and _supports_thinking(model):
            payload["reasoning_effort"] = _MISTRAL_REASONING_EFFORT
        if provider == "openrouter":
            # OBJ-8 Lote A2 (src/openrouter_options.py): apply the endpoint's
            # saved OpenRouter provider preferences, opt-in web search /
            # native fallback, and set usage.include=True so Lote A1's
            # real-cost accounting has something to read back. This sync
            # call path only ever receives a bare url/model, not a saved
            # endpoint config, so the real `endpoint_id` is recovered by
            # matching `url` against a configured endpoint's `base_url`
            # (see `_endpoint_id_for_url`) -- `None` when nothing matches.
            # Never fails the call over an options bug.
            try:
                from src.openrouter_options import apply_openrouter_payload
                apply_openrouter_payload(payload, provider=provider, endpoint_id=_endpoint_id_for_url(url), model=model)
            except Exception as exc:  # noqa: BLE001 -- an options bug must never break a chat call
                logger.debug("openrouter_options: apply_openrouter_payload failed for %s: %s", model, exc)
            if _openrouter_anthropic_cache_hints_applicable(provider, model):
                _apply_openrouter_anthropic_cache_hints(payload, tools=None)
    # A local backend reasons before it answers, and the shared default was
    # written for hosted APIs -- see `resolve_timeout`.
    timeout = resolve_timeout(url, timeout)
    try:
        note_model_activity(target_url, model)
        r = httpx_post_kimi_aware(target_url, h, json=payload, timeout=timeout)
    except Exception as e:
        raise HTTPException(502, f"POST {target_url} failed: {e}")
    if not r.is_success:
        raise HTTPException(502, f"Upstream {target_url} -> {r.status_code}: {r.text}")
    data = r.json()
    try:
        if provider == "anthropic":
            response = _parse_anthropic_response(data)
        elif provider == "ollama":
            response = _parse_ollama_response(data)
        else:
            msg = data["choices"][0]["message"]
            content = msg.get("content")
            if isinstance(content, list):
                # Mistral structured content — extract thinking + text
                text_part, thinking_part = _normalize_mistral_content(content)
                if thinking_part:
                    response = thinking_part + "\n\n" + (text_part or "")
                else:
                    response = text_part or reasoning_as_answer(msg.get("reasoning_content"))
            else:
                response = content or reasoning_as_answer(msg.get("reasoning_content"))
    except Exception:
        raise HTTPException(502, f"Unexpected schema from {target_url}: {str(data)[:400]}")

    if empty_completion(response):
        response = _retry_empty_completion(
            target_url, h, payload, provider, model, timeout,
        )
    _set_cached_response(cache_key, response)
    return response


def _retry_empty_completion(target_url: str, headers: Dict, payload: Dict,
                            provider: str, model: str, timeout) -> str:
    """One more try when a backend answered with nothing, then give up loudly.

    A local backend whose slot has gone bad answers in under a second with an
    empty content field, `finish_reason` "stop" and a few characters of
    punctuation in the reasoning channel -- and it keeps doing it, because the
    poisoned prompt cache is what it is reading from. Measured on this
    machine: the same six bytes on every request until the engine was
    restarted, after which the same question answered correctly in 3.7 s.

    So the retry asks llama.cpp to rebuild the prompt instead of reusing the
    cached one (`cache_prompt: false`). If that still comes back empty the
    fault is not transient, and an exception is the right answer: a turn that
    renders as a blank message after minutes of waiting tells the user nothing
    and leaves them to guess, while an error names what happened and what to
    do about it.
    """
    logger.warning("Empty completion from %s (%s); retrying without the prompt "
                   "cache and without reasoning", target_url, model)
    retry = dict(payload)
    retry["cache_prompt"] = False
    # The other way to answer with nothing is to spend the whole budget
    # thinking: measured on the local 27B, a two-sentence question took 37.2 s,
    # produced 1,752 characters of reasoning, hit the token ceiling and
    # returned an empty answer -- while the same question with reasoning off
    # answered correctly in 5.5 s. Whatever the model was working through, it
    # did not get to say it, so the retry does not pay for it twice.
    _suppress_thinking(retry, model)
    try:
        r = httpx_post_kimi_aware(target_url, headers, json=retry, timeout=timeout)
        if r.is_success:
            data = r.json()
            if provider == "ollama":
                text = _parse_ollama_response(data)
            else:
                message = data["choices"][0]["message"]
                text = message.get("content") or ""
            if not empty_completion(text):
                logger.info("The retry answered; the prompt cache was the problem")
                return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("The retry of an empty completion also failed: %s", exc)

    raise HTTPException(502, (
        f"{model} returned an empty answer twice, including once with its "
        f"prompt cache bypassed. The engine behind {target_url} is most "
        f"likely in a bad state; restarting it from Settings -> Local models "
        f"clears this."
    ))


def _candidate_is_configured(candidate) -> bool:
    return bool(
        isinstance(candidate, (tuple, list))
        and len(candidate) == 3
        and isinstance(candidate[0], str)
        and candidate[0].strip()
        and isinstance(candidate[1], str)
        and candidate[1].strip()
    )


def _safe_route_descriptor(value) -> dict:
    value = value if isinstance(value, dict) else {}
    endpoint_id = value.get("endpoint_id")
    endpoint_label = value.get("endpoint_label")
    endpoint_cost_tracked = value.get("endpoint_cost_tracked")
    return {
        "endpoint_id": endpoint_id if isinstance(endpoint_id, str) and endpoint_id else None,
        "endpoint_label": (
            endpoint_label
            if isinstance(endpoint_label, str) and endpoint_label.strip()
            else "Selected route"
        ),
        "endpoint_cost_tracked": (
            endpoint_cost_tracked
            if isinstance(endpoint_cost_tracked, bool)
            else None
        ),
    }


def _dedupe_model_candidates_with_descriptors(candidates, descriptors=None):
    """Dedupe routes and their parallel non-secret descriptors together."""

    seen = []
    out = []
    out_descriptors = []
    descriptors = list(descriptors or [])
    for index, candidate in enumerate(candidates or []):
        if not _candidate_is_configured(candidate):
            continue
        route = (candidate[0], candidate[1], candidate[2] or {})
        if any(route == prior for prior in seen):
            continue
        seen.append(route)
        out.append(candidate)
        raw_descriptor = descriptors[index] if index < len(descriptors) else {}
        out_descriptors.append(_safe_route_descriptor(raw_descriptor))
    return out, out_descriptors


def dedupe_model_candidates(candidates):
    """Filter malformed entries and drop a later repeat of an already-seen
    ``(url, model, headers)`` route, preserving order (first occurrence wins).

    The chain is the primary target followed by any caller-authorized
    fallbacks.  A fallback that repeats the session's current model would
    otherwise make the chain re-attempt the very route that just failed: a
    wasted round-trip plus a spurious ``fallback`` notice for a switch that did
    not happen. Credentials are part of route identity: two configured
    endpoints may intentionally use the same provider URL/model with different
    keys, and rate limiting on one must not discard the other candidate.
    """
    out, _descriptors = _dedupe_model_candidates_with_descriptors(candidates)
    return out


def llm_call_with_fallback(candidates, messages, **kwargs) -> str:
    """Sync `llm_call` with an ordered fallback chain.

    `candidates` is a list of (url, model, headers). The first one that returns
    without an exception wins. Connection / 5xx-style failures fall through to
    the next candidate. The dead-host cooldown inside `llm_call` makes repeat
    attempts at an offline primary effectively free.
    """
    cands = dedupe_model_candidates(candidates)
    if not cands:
        raise HTTPException(503, "No model endpoint configured")
    last_err = None
    for i, (url, model, headers) in enumerate(cands):
        try:
            return llm_call(url, model, messages, headers=headers, **kwargs)
        except Exception as e:
            last_err = e
            tag = "primary" if i == 0 else "candidate"
            logger.warning(f"[fallback] {tag} {model} failed ({type(e).__name__}); trying next")
            continue
    raise last_err if last_err else HTTPException(503, "All fallback candidates failed")


#: ADP-03 guarantee (d): a subscription-backed endpoint (chatgpt_subscription,
#: copilot) must never silently fall back to a paid API candidate just
#: because it returned 401/403 -- an auth failure is not "try the next one",
#: it is "this credential is wrong/expired" and resending the exact same
#: request against a DIFFERENT (possibly paid) provider is exactly the
#: silent-cost-shift the masterplan forbids. Mirrors
#: `src.foreground_model_routing.FOREGROUND_AVAILABILITY_STATUSES`'s own
#: exclusion of 401/403 from its eligible-for-fallback set, so both the
#: foreground and background/task fallback chains agree on this one status
#: pair without importing each other.
_NEVER_FALLBACK_STATUSES = frozenset({401, 403})


async def llm_call_async_with_fallback(candidates, messages, **kwargs) -> str:
    """Async variant of `llm_call_with_fallback` — same semantics.

    Every candidate after the first is a FALLBACK, not a retry of the same
    request against the same credential -- so a 401/403 (`_nonstream_error_status`,
    the same status-extraction this module's route-fallback helper already
    uses) is re-raised immediately instead of being treated like a transient
    503: see `_NEVER_FALLBACK_STATUSES`.
    """
    cands = dedupe_model_candidates(candidates)
    if not cands:
        raise HTTPException(503, "No model endpoint configured")
    last_err = None
    for i, (url, model, headers) in enumerate(cands):
        try:
            return await llm_call_async(url, model, messages, headers=headers, **kwargs)
        except Exception as e:
            status = _nonstream_error_status(e)
            if status in _NEVER_FALLBACK_STATUSES:
                logger.warning(
                    f"[fallback] {model} failed with {status}; refusing to fall "
                    "back to a different candidate on an auth failure")
                raise
            last_err = e
            tag = "primary" if i == 0 else "candidate"
            logger.warning(f"[fallback] {tag} {model} failed ({type(e).__name__}); trying next")
            continue
    raise last_err if last_err else HTTPException(503, "All fallback candidates failed")


def _nonstream_error_status(error: Exception) -> Optional[int]:
    """Normalize a non-stream provider failure for explicit fallback policy."""

    status = getattr(error, "status_code", None)
    if not isinstance(status, bool) and status is not None:
        return _normalize_http_status(status)
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return 503
    if isinstance(error, httpx.ReadTimeout):
        return 504
    return None


async def _vision_filter_for_route(url, model, messages):
    """A route that cannot see never receives image blocks: each one becomes
    its cached description or a short placeholder (src/vision_routing.py).
    Runs per candidate, so a fallback to a text-only model is covered too.
    The caller's list is not modified."""
    try:
        from src.vision_routing import strip_images_for_text_only_route
        return await strip_images_for_text_only_route(url, model, messages)
    except Exception as exc:  # noqa: BLE001 - never lose a turn over the filter
        logger.debug("vision history filter skipped: %s", exc)
        return messages


async def llm_call_async_with_route_fallback(
    candidates,
    messages,
    *,
    fallback_statuses,
    **kwargs,
):
    """Call an ordered non-stream route chain and return route provenance.

    Unlike the legacy utility helper, this advances only for an explicitly
    eligible status.  A successful empty response still commits the current
    candidate; empty output is not availability evidence.  The third return
    value is the provider-reported model when available, otherwise the exact
    configured candidate model.
    """

    raw_candidates = list(candidates or [])
    if not raw_candidates or not _candidate_is_configured(raw_candidates[0]):
        raise _FallbackIneligibleHTTPException(400, "Selected model endpoint is not configured")
    candidate_request_factory = kwargs.pop("candidate_request_factory", None)
    cands = dedupe_model_candidates(raw_candidates)
    if not cands:
        raise HTTPException(503, "No model endpoint configured")
    eligible_statuses = frozenset(fallback_statuses or ())
    for index, candidate in enumerate(cands):
        url, model, headers = candidate
        try:
            candidate_messages = messages
            candidate_kwargs = kwargs
            if candidate_request_factory is not None:
                request = candidate_request_factory(index, url, model, headers) or {}
                if hasattr(request, "__await__"):
                    request = await request
                candidate_messages = request.get("messages", messages)
                candidate_kwargs = {**kwargs, **(request.get("kwargs") or {})}
            candidate_kwargs = {
                **candidate_kwargs,
                "availability_only_transport": True,
            }
            candidate_messages = await _vision_filter_for_route(url, model, candidate_messages)
            response = await llm_call_async(
                url,
                model,
                candidate_messages,
                headers=headers,
                return_model_metadata=True,
                **candidate_kwargs,
            )
            actual_model = model
            if (
                isinstance(response, tuple)
                and len(response) == 2
                and isinstance(response[0], str)
            ):
                response, reported_model = response
                if isinstance(reported_model, str) and reported_model.strip():
                    actual_model = reported_model.strip()
            return response, candidate, actual_model
        except Exception as error:
            if getattr(error, "fallback_eligible", None) is False:
                raise
            status = _nonstream_error_status(error)
            if index >= len(cands) - 1 or status not in eligible_statuses:
                raise
            tag = "primary" if index == 0 else "candidate"
            logger.warning(
                "[fallback] %s %s failed with eligible status %s; trying next",
                tag,
                model,
                status,
            )

    raise HTTPException(503, "All fallback candidates failed")


async def llm_call_async(
    url: str,
    model: str,
    messages: List[Dict],
    temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
    max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS,
    headers: Optional[Dict] = None,
    timeout: int = LLMConfig.STREAM_TIMEOUT,
    max_retries: int = LLMConfig.MAX_RETRIES,
    prompt_type: Optional[str] = None,
    session_id: Optional[str] = None,
    workload: str = "foreground",
    availability_only_transport: bool = False,
    return_model_metadata: bool = False,
    response_schema: Optional[Dict] = None,
    pin_public_dns: bool = False,
    on_outcome_unknown: Optional[Callable[[int, float], None]] = None,
) -> str | tuple[str, str]:
    """Traced wrapper around ``_llm_call_async_impl`` (LLM-TRACE-01).

    This is the narrowest point that sees BOTH the final request this
    caller is making (url/model/messages/temperature/max_tokens — the
    non-streaming path takes no ``tools``) and the fully assembled response
    (the impl's return value is already the complete text). Wrapping here
    instead of littering every provider branch inside the impl means one
    place records every non-streaming call regardless of which provider
    handled it, and a bug in tracing can never affect the call itself
    (record happens after the real work is done, in its own try/except).
    """
    _t0 = time.time()
    _err: Optional[str] = None
    _text = ""
    _model_out = model
    try:
        result = await _llm_call_async_impl(
            url, model, messages,
            temperature=temperature, max_tokens=max_tokens, headers=headers,
            timeout=timeout, max_retries=max_retries, prompt_type=prompt_type,
            session_id=session_id, workload=workload,
            availability_only_transport=availability_only_transport,
            return_model_metadata=return_model_metadata,
            response_schema=response_schema, pin_public_dns=pin_public_dns,
            on_outcome_unknown=on_outcome_unknown,
        )
        if isinstance(result, tuple):
            _text, _model_out = result[0], result[1]
        else:
            _text = result
        return result
    except Exception as exc:
        _err = str(exc)
        raise
    finally:
        try:
            from src import llm_trace
            llm_trace.record_call(
                session_id=session_id,
                endpoint_url=url,
                model=_model_out,
                request={
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_schema": response_schema,
                    "headers": headers,
                },
                response_text=_text if isinstance(_text, str) else "",
                duration_ms=(time.time() - _t0) * 1000.0,
                error=_err,
            )
        except Exception:
            logger.debug("[llm_trace] non-streaming record failed", exc_info=True)


async def _llm_call_async_impl(
    url: str,
    model: str,
    messages: List[Dict],
    temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
    max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS,
    headers: Optional[Dict] = None,
    timeout: int = LLMConfig.STREAM_TIMEOUT,
    max_retries: int = LLMConfig.MAX_RETRIES,
    prompt_type: Optional[str] = None,
    session_id: Optional[str] = None,
    workload: str = "foreground",
    availability_only_transport: bool = False,
    return_model_metadata: bool = False,
    response_schema: Optional[Dict] = None,
    pin_public_dns: bool = False,
    on_outcome_unknown: Optional[Callable[[int, float], None]] = None,
) -> str | tuple[str, str]:
    """Asynchronous LLM call using httpx with connection pooling, timeout, retry logic, and performance logging.

    ``response_schema`` is a JSON Schema the answer must obey. Native Ollama
    enforces it while decoding (``format`` on /api/chat); every other provider
    ignores it and never sees it, so callers keep their own parsing as a net.

    Retries (CALL-06, spec §34.5) are classified by ``src.retry_policy`` rather
    than retried on optimism: 429/503 honour ``Retry-After``, other 5xx use
    jittered backoff, and 4xx never retry. A read timeout or a reset that
    happens after the request body was already written is ``outcome_unknown``
    — this call still retries it (a plain completion's only "effect" is
    provider-side tokens spent, which §34.5 accepts as retryable), but calls
    ``on_outcome_unknown(attempt, elapsed_seconds)`` first so a caller can log
    or account for the fact that the first attempt may have completed
    upstream. A tool wrapping a call with a REMOTE EFFECT (sending mail,
    posting, anything not just "tokens spent") must NOT reuse this
    auto-retry-on-outcome-unknown behaviour: classify with
    ``src.retry_policy.classify_http`` directly and, on
    ``RetryClass.OUTCOME_UNKNOWN``, reconcile (query the service for the
    effect's own idempotency key / status) or surface it for a human instead
    of resending blindly.
    """
    if str(url or '').startswith('faustus-cli://'):
        from src.cli_model import complete_async
        text = await complete_async(url, model, messages, headers, timeout)
        return (text, model) if return_model_metadata else text
    # A direct API-chat endpoint supplied by a token holder persists this
    # private marker with the session. It is consumed here and NEVER sent to
    # the provider. This keeps resumed sessions protected from DNS rebinding.
    clean_headers = dict(headers or {})
    for _header_name in list(clean_headers):
        if str(_header_name).lower() == "x-faustus-public-dns-pin":
            _raw_pin = str(clean_headers.pop(_header_name) or "").strip().lower()
            pin_public_dns = pin_public_dns or _raw_pin in {
                "1", "true", "yes", "on",
            }
    headers = clean_headers or None

    # Same reroute as stream_llm: Ollama's /v1 ignores `think`, so a
    # thinking-capable model (qwen3.5, gemma…) would spend the whole
    # num_predict budget reasoning and return an empty `content` (seen live:
    # the diff reviewer answered nothing in 17 s). The native /api/chat
    # honours think=false.
    _native_think_off = False
    # Saved per-model load defaults (num_ctx / num_gpu / keep_alive) are the
    # only overrides this non-streaming path carries; like `think` they need
    # the native endpoint, so they take part in the routing decision.
    _model_defaults = _clean_gen_overrides(_model_load_defaults(url, model))
    _routed = _route_for_gen_overrides(url, _model_defaults or None, model)
    if _routed != url:
        caps = _ollama_model_caps(url, model)
        _native_think_off = caps is not None and "thinking" in caps
        logger.info("Ollama /v1 -> native /api/chat for %s (non-streaming call, think=%s)", model,
                    False if _native_think_off else None)
        url = _routed
    # And the same reroute for a constrained answer: `format` is native-only,
    # so a schema sent to /v1 would be dropped in silence and we would believe
    # the JSON was guaranteed when nothing guaranteed it. Move the request to
    # the endpoint that honours it, or send no schema at all.
    if isinstance(response_schema, dict) and response_schema and _structured_output_enabled():
        _routed_schema = _route_for_response_schema(url, model)
        if _routed_schema != url:
            caps = _ollama_model_caps(url, model)
            _native_think_off = caps is not None and "thinking" in caps
            logger.info("Ollama /v1 -> native /api/chat for %s (constrained JSON decoding, think=%s)",
                        model, False if _native_think_off else None)
            url = _routed_schema
    provider = _detect_provider(url)
    messages_copy = _sanitize_llm_messages(messages, provider=provider)

    # Consolidate multiple system messages into one at the start.
    sys_parts = []
    non_sys = []
    for m in messages_copy:
        if m.get("role") == "system":
            sys_parts.append(m.get('content') or '')
        else:
            non_sys.append(m)
    if sys_parts:
        messages_copy = [{"role": "system", "content": "\n\n".join(sys_parts)}] + non_sys
    else:
        messages_copy = non_sys

    schema = _resolve_response_schema(url, response_schema)
    cache_key = _get_cache_key(
        url, model, messages_copy, temperature, max_tokens, headers=headers,
        response_schema=schema,
    )
    cached_response = _get_cached_response(cache_key)
    if cached_response:
        logger.debug(f"Returning cached response for key: {cache_key}")
        if return_model_metadata:
            return cached_response, (_get_cached_response_model(cache_key) or model)
        return cached_response

    if provider == "chatgpt-subscription":
        # ChatGPT/Codex requires streamed Responses requests even for callers
        # that want a plain string (auto-title, memory extraction, etc.).
        # Reuse stream_llm's validated Codex SSE path and collect deltas.
        parts: List[str] = []
        actual_model = model
        async for chunk in stream_llm(
            url,
            model,
            messages_copy,
            temperature=temperature,
            max_tokens=max_tokens,
            headers=headers,
            timeout=timeout,
            workload=workload,
        ):
            event_is_error = False
            for line in str(chunk).splitlines():
                if line.startswith("event:"):
                    event_is_error = line[6:].strip() == "error"
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                if raw == "[DONE]":
                    response = "".join(parts)
                    _set_cached_response(
                        cache_key,
                        response,
                        actual_model=actual_model,
                    )
                    return (
                        (response, actual_model)
                        if return_model_metadata
                        else response
                    )
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if event_is_error or data.get("error") or (data.get("status") and data.get("text")):
                    status = int(data.get("status") or 502)
                    text = data.get("text") or data.get("error") or "ChatGPT Subscription request failed"
                    error_type = (
                        _FallbackIneligibleHTTPException
                        if data.get("fallback_eligible") is False
                        else HTTPException
                    )
                    raise error_type(status, text)
                if data.get("type") == "model_actual":
                    reported_model = data.get("model")
                    if isinstance(reported_model, str) and reported_model.strip():
                        actual_model = reported_model.strip()
                delta = data.get("delta")
                if isinstance(delta, str):
                    parts.append(delta)
        response = "".join(parts)
        _set_cached_response(cache_key, response, actual_model=actual_model)
        return (response, actual_model) if return_model_metadata else response

    if provider == "anthropic":
        target_url = _normalize_anthropic_url(url)
        h = _build_anthropic_headers(headers)
        payload = _build_anthropic_payload(model, messages_copy, temperature, max_tokens)
    elif provider == "ollama":
        target_url = _normalize_ollama_url(url)
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        payload = _build_ollama_payload(
            model, messages_copy, temperature, max_tokens,
            stream=False, num_ctx=get_context_length(url, model),
            response_schema=schema,
        )
        if _model_defaults:
            _apply_gen_overrides_ollama(payload, _model_defaults)
        if _native_think_off:
            payload["think"] = False
    else:
        target_url = _normalize_openai_chat_url(url)
        h = _provider_headers(provider, headers)
        if provider == "copilot":
            from src.copilot import apply_request_headers
            apply_request_headers(h, messages_copy)
        payload = {
            "model": model,
            "messages": messages_copy,
            "temperature": temperature,
        }
        if _omit_temperature(provider, model):
            payload.pop("temperature", None)
        if max_tokens and max_tokens > 0:
            tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
            payload[tok_key] = max_tokens
        # Suppress thinking for qwen3/gemma4 on Ollama /v1 — same as stream_llm.
        if _is_ollama_openai_compat_url(url) and _supports_thinking(model):
            payload["think"] = False
        # And the llama-server / vLLM spelling of the same default. Without it
        # the chat template's own default applies -- thinking ON for qwen3.x
        # -- and every helper call (titles, summaries, a swarm item, a
        # podcast script) reasoned for minutes before its first word, or
        # spent its whole budget thinking and returned no content at all.
        if (_supports_thinking(model) and _is_self_hosted_openai_compatible(url)
                and not _is_local_ollama_target(url)):
            _ctk = payload.get("chat_template_kwargs")
            if not isinstance(_ctk, dict):
                _ctk = {}
                payload["chat_template_kwargs"] = _ctk
            _ctk.setdefault("enable_thinking", False)
        if provider == "mistral" and _supports_thinking(model):
            payload["reasoning_effort"] = _MISTRAL_REASONING_EFFORT
        _apply_local_cache_affinity(payload, url, session_id)
        _apply_local_generation_stability(payload, target_url, model)
        _apply_openai_response_format(payload, url, schema, model=model)  # `url`: see llm_call
        # No `tools` here: this path is the tool-less completion helper.
        _suppress_thinking_for_small_talk(payload, model, messages_copy)
        if provider == "openrouter":
            # Same OpenRouter options application as llm_call (OBJ-8 Lote A2)
            # -- see that call site for the full rationale.
            try:
                from src.openrouter_options import apply_openrouter_payload
                apply_openrouter_payload(payload, provider=provider, endpoint_id=_endpoint_id_for_url(url), model=model)
            except Exception as exc:  # noqa: BLE001
                logger.debug("openrouter_options: apply_openrouter_payload failed for %s: %s", model, exc)
            if _openrouter_anthropic_cache_hints_applicable(provider, model):
                _apply_openrouter_anthropic_cache_hints(payload, tools=None)

    if _is_host_dead(target_url):
        raise HTTPException(503, f"Upstream {_host_key(target_url)} marked unreachable (cooldown active)")

    # Same reason as the sync path: a local backend reasons before it answers,
    # and the shared default was written for hosted APIs.
    call_timeout = _call_timeout(resolve_timeout(url, timeout))
    _pinned_ips = None
    _pinned_transport_type = None
    if pin_public_dns:
        try:
            from src.webhook_manager import (
                _PinnedAsyncTransport as _PinnedTransport,
                _validated_public_ips,
            )
            _pinned_ips = list(_validated_public_ips(target_url))
            _pinned_transport_type = _PinnedTransport
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            logger.warning("public DNS pinning failed closed for %s: %s",
                           _host_key(target_url), exc)
            raise HTTPException(503, "Could not establish a safe connection to the endpoint")
    def _annotate(exc: HTTPException, *, error_class: str, retryable: bool,
                   attempts: int) -> HTTPException:
        """Attach the §34.5 error_class/retryable/attempts fields to the
        exception this loop raises, without changing its type — fallback
        eligibility elsewhere keys off `isinstance`/`fallback_eligible`, and
        HTTPException happily carries extra attributes."""
        exc.error_class = error_class
        exc.retryable = retryable
        exc.attempts = attempts
        return exc

    _budget = RetryBudget(LLMConfig.RETRY_TIME_BUDGET)
    _budget.start()
    attempt = 0
    # Set once this call has already restarted its engine (below) — never a
    # second time for the same call, so an engine that dies again right
    # after coming back fails normally instead of looping.
    _engine_wait_used = False
    while attempt < max_retries:
        attempt += 1
        start = time.time()
        try:
            async with _local_model_slot(target_url, model, workload):
                note_model_activity(target_url, model)
                if _pinned_ips and _pinned_transport_type is not None:
                    # The URL keeps the original hostname for Host/SNI; only
                    # the TCP destination is pinned to the already-validated
                    # public address. Redirects and proxy environment are off.
                    _pin = _pinned_ips[(attempt - 1) % len(_pinned_ips)]
                    _transport = _pinned_transport_type(_pin)
                    async with httpx.AsyncClient(
                        transport=_transport,
                        follow_redirects=False,
                        trust_env=False,
                    ) as client:
                        r = await httpx_post_kimi_aware_async(
                            client, target_url, h, json=payload,
                            timeout=call_timeout,
                        )
                else:
                    client = _get_http_client()
                    r = await httpx_post_kimi_aware_async(
                        client, target_url, h, json=payload,
                        timeout=call_timeout,
                    )
            duration = time.time() - start
            if not r.is_success:
                friendly = _format_upstream_error(r.status_code, r.text, target_url)
                classification = classify_http(status=r.status_code, headers=r.headers)
                err_class = _retry_error_class(status=r.status_code)
                logger.warning(
                    f"LLM async call to {target_url} failed in {duration:.2f}s "
                    f"(attempt {attempt}, {classification}): HTTP {r.status_code} {friendly}"
                )
                # retry_now (429/503 honouring Retry-After, 502/504) and
                # retry_backoff (any other 5xx with no Retry-After) both
                # retry here — the difference between the two classes is
                # only whether a Retry-After is honoured, which parse_retry_after
                # already handles regardless of which class this is.
                if (classification in (RetryClass.RETRY_NOW, RetryClass.RETRY_BACKOFF)
                        and attempt < max_retries and not _budget.exhausted()):
                    _retry_after = parse_retry_after(r.headers)
                    _wait = _retry_delay(attempt, retry_after=_retry_after)
                    if _retry_after is not None:
                        logger.info(
                            f"LLM async call to {target_url}: server busy (HTTP {r.status_code}), "
                            f"retrying in {_wait:.1f}s (Retry-After honoured)"
                        )
                    await asyncio.sleep(_wait)
                    continue
                raise _annotate(
                    HTTPException(r.status_code, friendly),
                    error_class=err_class,
                    retryable=classification is not RetryClass.NO_RETRY,
                    attempts=attempt,
                )
            logger.info(f"LLM async call to {target_url} succeeded in {duration:.2f}s (attempt {attempt})")
            _clear_host_dead(target_url)
            data = r.json()
            if provider == "ollama" and isinstance(data, dict):
                # Deep Research is all non-streamed calls; without this the
                # learned speed would only ever come from the chat.
                remember_local_speed(model, data.get("eval_count"), data.get("eval_duration"))
            if isinstance(data, dict) and data.get("error"):
                provider_error = data["error"]
                status = _provider_stream_error_status(provider_error, default=400)
                if isinstance(provider_error, dict):
                    detail = provider_error.get("message") or provider_error.get("type") or str(provider_error)
                else:
                    detail = str(provider_error)
                raise HTTPException(status, detail or "Upstream request failed")
            try:
                reported_model = data.get("model") if isinstance(data, dict) else None
                actual_model = (
                    reported_model.strip()
                    if isinstance(reported_model, str) and reported_model.strip()
                    else model
                )
                if provider == "anthropic":
                    response = _parse_anthropic_response(data)
                elif provider == "ollama":
                    response = _parse_ollama_response(data)
                else:
                    msg = data["choices"][0]["message"]
                    content = msg.get("content")
                    if isinstance(content, list):
                        # Mistral structured content — extract thinking + text
                        # (same contract as llm_call / stream_llm; see #5435).
                        text_part, thinking_part = _normalize_mistral_content(content)
                        if thinking_part:
                            response = thinking_part + "\n\n" + (text_part or "")
                        else:
                            response = text_part or reasoning_as_answer(msg.get("reasoning_content"))
                    else:
                        response = content or reasoning_as_answer(msg.get("reasoning_content"))
                _set_cached_response(
                    cache_key,
                    response,
                    actual_model=actual_model,
                )
                return (
                    (response, actual_model)
                    if return_model_metadata
                    else response
                )
            except HTTPException:
                raise
            except Exception:
                raise _FallbackIneligibleHTTPException(
                    502,
                    f"Unexpected schema from {target_url}: {str(data)[:400]}",
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            classification = classify_http(exc=e)  # connect-phase: retry_backoff
            err_class = _retry_error_class(exc=e)
            _cooled = _mark_host_dead(target_url)
            duration = time.time() - start
            _tail = f" — host cooled for {DEAD_HOST_COOLDOWN:.0f}s" if _cooled else " — transient, will retry"
            logger.warning(f"LLM async connect to {target_url} failed after {duration:.2f}s: {e}{_tail}")
            if _cooled or attempt >= max_retries or _budget.exhausted():
                if not _engine_wait_used:
                    _engine_wait_used = True
                    from src import engine_swap
                    if engine_swap.restartable_engine_for_url(target_url) is not None:
                        logger.warning(
                            "LLM async call to %s: retries exhausted on a managed engine — "
                            "starting it again before failing the turn",
                            _host_key(target_url),
                        )
                        if await engine_swap.recover_after_connect_failure(target_url):
                            _clear_host_dead(target_url)
                            # Fresh budget/attempt for the one retry this
                            # earns: the time already spent waiting must not
                            # eat into the normal retry budget.
                            _budget = RetryBudget(LLMConfig.RETRY_TIME_BUDGET)
                            _budget.start()
                            attempt -= 1
                            continue
                raise _annotate(
                    HTTPException(503, f"Cannot reach {_host_key(target_url)}: {e}"),
                    error_class=err_class,
                    retryable=classification is not RetryClass.NO_RETRY,
                    attempts=attempt,
                )
            await asyncio.sleep(_retry_delay(attempt))
        except httpx.ReadTimeout as e:
            duration = time.time() - start
            classification = classify_http(exc=e)  # outcome_unknown: body was already written
            err_class = _retry_error_class(exc=e)
            logger.warning(
                f"LLM async read timed out after {duration:.2f}s (attempt {attempt}): {e} "
                f"— the request body was already sent; the call may have completed "
                f"upstream even though no response arrived"
            )
            if on_outcome_unknown is not None:
                try:
                    on_outcome_unknown(attempt, duration)
                except Exception:
                    logger.debug("on_outcome_unknown hook raised", exc_info=True)
            if attempt >= max_retries or _budget.exhausted():
                raise _annotate(
                    HTTPException(
                        504,
                        f"POST {target_url} timed out after {max_retries} attempts "
                        f"(request may have completed upstream; outcome unknown)",
                    ),
                    error_class=err_class, retryable=False, attempts=attempt,
                )
            # A plain completion's only effect is provider-side tokens spent,
            # which §34.5 accepts as retryable even under outcome_unknown —
            # see the on_outcome_unknown note in this function's docstring. A
            # caller wrapping a REMOTE EFFECT must not copy this: classify
            # with RetryClass.OUTCOME_UNKNOWN itself and reconcile instead of
            # resending blindly (QA-11).
            await asyncio.sleep(_retry_delay(attempt))
        except httpx.PoolTimeout as e:
            duration = time.time() - start
            classification = classify_http(exc=e)  # retry_backoff: never got a connection
            err_class = _retry_error_class(exc=e)
            logger.warning(f"LLM async connection pool timed out after {duration:.2f}s: {e}")
            if availability_only_transport:
                raise _annotate(
                    HTTPException(504, f"POST {target_url} could not acquire an upstream connection"),
                    error_class=err_class, retryable=False, attempts=attempt,
                )
            if attempt >= max_retries or _budget.exhausted():
                raise _annotate(
                    HTTPException(504, f"POST {target_url} timed out after {max_retries} attempts"),
                    error_class=err_class,
                    retryable=classification is not RetryClass.NO_RETRY,
                    attempts=attempt,
                )
            await asyncio.sleep(_retry_delay(attempt))
        except httpx.WriteTimeout as e:
            duration = time.time() - start
            classification = classify_http(exc=e)  # still sending: retry_backoff
            err_class = _retry_error_class(exc=e)
            logger.warning(f"LLM async upstream timeout after {duration:.2f}s: {e}")
            if availability_only_transport:
                raise _annotate(
                    _FallbackIneligibleHTTPException(504, f"POST {target_url} failed during request delivery"),
                    error_class=err_class, retryable=False, attempts=attempt,
                )
            if attempt >= max_retries or _budget.exhausted():
                raise _annotate(
                    HTTPException(504, f"POST {target_url} timed out after {max_retries} attempts"),
                    error_class=err_class,
                    retryable=classification is not RetryClass.NO_RETRY,
                    attempts=attempt,
                )
            await asyncio.sleep(_retry_delay(attempt))
        except httpx.ProtocolError as e:
            duration = time.time() - start
            # RemoteProtocolError (server reset/violated protocol mid-stream)
            # classifies outcome_unknown; a local one (malformed request on
            # our side) classifies retry_backoff like any unrecognised
            # transport failure — see retry_policy.classify_http.
            classification = classify_http(exc=e)
            err_class = _retry_error_class(exc=e)
            logger.warning(f"LLM async protocol failure after {duration:.2f}s: {e}")
            if classification is RetryClass.OUTCOME_UNKNOWN and on_outcome_unknown is not None:
                try:
                    on_outcome_unknown(attempt, duration)
                except Exception:
                    logger.debug("on_outcome_unknown hook raised", exc_info=True)
            if availability_only_transport:
                raise _annotate(
                    _FallbackIneligibleHTTPException(502, f"POST {target_url} failed with a protocol error"),
                    error_class=err_class, retryable=False, attempts=attempt,
                )
            if attempt >= max_retries or _budget.exhausted():
                raise _annotate(
                    HTTPException(502, f"POST {target_url} failed after {max_retries} attempts: {e}"),
                    error_class=err_class,
                    retryable=classification is not RetryClass.NO_RETRY,
                    attempts=attempt,
                )
            await asyncio.sleep(_retry_delay(attempt))
        except httpx.NetworkError as e:
            duration = time.time() - start
            # ReadError (reset while reading the response) classifies
            # outcome_unknown; WriteError/CloseError classify retry_backoff.
            classification = classify_http(exc=e)
            err_class = _retry_error_class(exc=e)
            logger.warning(f"LLM async network failure after {duration:.2f}s: {e}")
            if classification is RetryClass.OUTCOME_UNKNOWN and on_outcome_unknown is not None:
                try:
                    on_outcome_unknown(attempt, duration)
                except Exception:
                    logger.debug("on_outcome_unknown hook raised", exc_info=True)
            if availability_only_transport:
                raise _annotate(
                    _FallbackIneligibleHTTPException(502, f"POST {target_url} failed with a network error"),
                    error_class=err_class, retryable=False, attempts=attempt,
                )
            if attempt >= max_retries or _budget.exhausted():
                raise _annotate(
                    HTTPException(502, f"POST {target_url} failed after {max_retries} attempts: {e}"),
                    error_class=err_class,
                    retryable=classification is not RetryClass.NO_RETRY,
                    attempts=attempt,
                )
            await asyncio.sleep(_retry_delay(attempt))
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else 502
            raise HTTPException(status, str(e))
        except httpx.RequestError as e:
            duration = time.time() - start
            logger.warning(f"LLM async request configuration failed after {duration:.2f}s: {e}")
            raise _FallbackIneligibleHTTPException(
                502,
                f"POST {target_url} could not be configured: {e}",
            )

def _stream_target_url(url: str) -> str:
    provider = _detect_provider(url)
    if provider == "anthropic":
        return _normalize_anthropic_url(url)
    if provider == "ollama":
        return _normalize_ollama_url(url)
    if provider == "chatgpt-subscription":
        return _normalize_chatgpt_subscription_url(url)
    return _normalize_openai_chat_url(url)


# Sampling/runtime knobs a user may pin per chat session (model controls in
# the chat UI / slash commands). Only these keys are honoured; anything else is
# dropped so a client cannot smuggle arbitrary provider fields.
GEN_OVERRIDE_KEYS = frozenset({"top_p", "top_k", "min_p", "seed", "think", "num_ctx", "num_gpu",
                               "repeat_penalty", "presence_penalty", "frequency_penalty",
                               "reasoning_effort", "reasoning_budget", "keep_alive", "main_gpu",
                               # per-model block of further Ollama `options`
                               # (src/model_load_options.EXTRA_OPTION_KEYS)
                               "extra"})

# Ollama `options` / top-level knobs with no OpenAI equivalent: a request that
# carries one of these has to go to the native /api/chat.
_OLLAMA_NATIVE_ONLY_KEYS = ("top_k", "min_p", "repeat_penalty", "num_ctx", "num_gpu", "keep_alive", "main_gpu", "extra")
_KEEP_ALIVE_RE = re.compile(r"^-?\d+(ms|s|m|h)?$")


def _clean_gen_overrides(overrides: Optional[Dict]) -> Dict:
    out: Dict = {}
    if not isinstance(overrides, dict):
        return out
    for k, v in overrides.items():
        if k not in GEN_OVERRIDE_KEYS or v is None or v == "":
            continue
        try:
            if k == "extra":
                # Re-validated here too: the saved block is trusted, but a
                # client can put anything in gen_overrides.
                from src.model_load_options import sanitize_extra
                cleaned = sanitize_extra(v) if isinstance(v, dict) else {}
                if cleaned:
                    out[k] = cleaned
            elif k in ("top_p", "min_p", "repeat_penalty", "presence_penalty", "frequency_penalty"):
                out[k] = float(v)
            elif k in ("top_k", "seed", "num_ctx", "num_gpu", "main_gpu", "reasoning_budget"):
                out[k] = int(v)
            elif k == "think":
                out[k] = bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "on", "yes")
            elif k == "reasoning_effort":
                if str(v) in ("minimal", "low", "medium", "high", "xhigh", "none"):
                    out[k] = str(v)
            elif k == "keep_alive":
                # Seconds as a number, or an Ollama duration ("10m", "-1").
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    out[k] = int(v)
                elif re.fullmatch(r"-?\d+", str(v).strip()):
                    # A bare number saved as text ("-1" = keep loaded forever) must go
                    # out as a number: Ollama's duration parser rejects "-1" with
                    # `time: missing unit in duration "-1"` (seen live, HTTP 400).
                    out[k] = int(str(v).strip())
                elif _KEEP_ALIVE_RE.match(str(v).strip()):
                    out[k] = str(v).strip()
        except (TypeError, ValueError):
            continue
    return out


def _model_load_defaults(url: str, model: str) -> Dict:
    """Per-model load defaults saved in Settings → Local models (the
    `model_load_options` setting, src/model_load_options.py): num_ctx /
    num_gpu / keep_alive for THIS model on THIS Ollama server. Empty for
    every other provider and whenever nothing was saved."""
    if not model or not url:
        return {}
    try:
        from src.model_load_options import resolve_for_request
        defaults = dict(resolve_for_request(url, model) or {})
    except Exception as e:  # noqa: BLE001 — a default is never worth failing a chat
        logger.debug("model load defaults unavailable for %s: %s", model, e)
        defaults = {}
    # The placement policy (fill card N first) adds main_gpu for a model that
    # fits that card, under a per-model pin. src/gpu_policy.py.
    if "main_gpu" not in defaults:
        try:
            from src.gpu_policy import preferred_main_gpu
            idx = preferred_main_gpu(url, model)
            if idx is not None:
                defaults["main_gpu"] = idx
        except Exception as e:  # noqa: BLE001
            logger.debug("gpu placement policy unavailable for %s: %s", model, e)
    # The `local_repeat_penalty_default`/`local_min_p_default` floor must
    # reach EVERY local Ollama model, not only ones with a saved per-model
    # `extra` block: previously these two knobs were only applied inside
    # `_apply_gen_overrides_ollama`, which only ever runs once a request has
    # already been routed to the native `/api/chat` surface — and routing
    # only rerouted a `/v1` request when `gen_overrides` already carried a
    # native-only key. A model with no saved `extra` and no per-turn override
    # therefore stayed on `/v1/chat/completions`, which Ollama serves with
    # its own defaults (repeat_penalty 1.0, min_p 0 — no repetition guard at
    # all) and can decode into sustained garbage at long context. Folding the
    # floor in here, ahead of the routing decision, makes it part of
    # `gen_overrides` early enough to both trigger the native reroute
    # (`_route_for_gen_overrides` treats `repeat_penalty`/`min_p` as
    # native-only keys) and land in the request either way. An explicit
    # per-model `extra` or per-turn override set later in the merge chain
    # (`_with_model_defaults`: defaults -> caller) still wins over this.
    if _is_local_ollama_target(url):
        # A saved per-model `extra` block (checked here, not just the
        # top-level knobs) already carries these two: don't let the floor
        # shadow it once `_apply_gen_overrides_ollama` applies `extra`
        # first and the named-key loop second — that loop writes THIS
        # dict's value over whatever `extra` set, so the floor must not be
        # added here when `extra` already answers for the field.
        extra_block = defaults.get("extra")
        extra_block = extra_block if isinstance(extra_block, dict) else {}
        if "repeat_penalty" not in defaults and "repeat_penalty" not in extra_block:
            defaults["repeat_penalty"] = _local_sampler_default("local_repeat_penalty_default", 1.05)
        if "min_p" not in defaults and "min_p" not in extra_block:
            defaults["min_p"] = _local_sampler_default("local_min_p_default", 0.05)
        # `local_top_p_default`/`local_top_k_default`: same floor idea as the
        # two above, for the sampler knobs that actually stop the rambling
        # (see `local_temperature_default`'s own comment in settings.py — the
        # temperature floor itself is applied earlier, in
        # `_stream_agent_loop_body`, where the explicit/default distinction
        # is already tracked). 0/empty (`_local_sampler_default_optional`
        # returns None) means "do not send".
        if "top_p" not in defaults and "top_p" not in extra_block:
            _top_p = _local_sampler_default_optional("local_top_p_default")
            if _top_p is not None:
                defaults["top_p"] = _top_p
        if "top_k" not in defaults and "top_k" not in extra_block:
            _top_k = _local_sampler_default_optional("local_top_k_default")
            if _top_k is not None:
                defaults["top_k"] = int(_top_k)
    return defaults


def _with_model_defaults(url: str, model: str, gen_overrides: Optional[Dict]) -> Optional[Dict]:
    """Saved per-model defaults UNDER the caller's explicit overrides: a
    `/ctx 8192` in the chat still beats a saved num_ctx of 32768.

    An active agent-run pin (src/run_model_pin.py) beats the saved keep_alive
    so a long bash cannot unload the weight; an explicit caller keep_alive
    still wins over the pin.
    """
    defaults = _model_load_defaults(url, model)
    caller = gen_overrides if isinstance(gen_overrides, dict) else {}
    caller_keep_alive = caller.get("keep_alive") not in (None, "")
    merged = dict(defaults)
    for k, v in caller.items():
        if v is None or v == "":
            continue
        merged[k] = v
    if not caller_keep_alive:
        try:
            from src.run_model_pin import keep_alive_override
            pinned = keep_alive_override(model)
            if pinned:
                merged["keep_alive"] = pinned
        except Exception:  # noqa: BLE001
            pass
    return merged if merged else gen_overrides


_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh")


def fit_reasoning_effort(value: str, accepted: Optional[tuple]) -> Optional[str]:
    """Map Faustus's effort vocabulary onto what the model's template accepts.

    ``accepted`` is the template's own list (``chat_helpers.
    llamacpp_reasoning_efforts``); None means the template does not restrict
    it and the value goes out as it is. "high" (Faustus's top level) becomes
    the template's top level when "high" itself is not accepted (Qwen3.8:
    "xhigh"); any other unknown value takes the nearest accepted level. Never
    invents a value the template did not list."""
    value = str(value or "").strip().lower()
    if not value or accepted is None:
        return value or None
    if value in accepted:
        return value
    try:
        want = _EFFORT_ORDER.index(value)
    except ValueError:
        return None
    ranked = sorted((v for v in accepted if v in _EFFORT_ORDER), key=_EFFORT_ORDER.index)
    if not ranked:
        return None
    if value == "high":
        return ranked[-1]
    return min(ranked, key=lambda v: (abs(_EFFORT_ORDER.index(v) - want), -_EFFORT_ORDER.index(v)))


def _mirror_thinking_budget(payload: Dict, url: str) -> None:
    """Send the reasoning budget under the name llama-server reads.

    Measured on the local llama-server (build 11040, Qwen3.8 27B): a request
    with `reasoning_budget: 40` reasoned 1.302 characters and hit its output
    cap still thinking; the same request with `thinking_budget_tokens: 40`
    reasoned 161 characters and answered. Every budget Faustus set (1024
    light, 4096 think, 16384 deep) had been ignored, and only the time-based
    watchdog stopped long reasoning. Both names go out: builds and servers
    that read `reasoning_budget` keep working."""
    budget = payload.get("reasoning_budget")
    if budget is None or "thinking_budget_tokens" in payload:
        return
    if not _is_self_hosted_openai_compatible(url) or _is_local_ollama_target(url):
        return
    try:
        value = int(budget)
    except (TypeError, ValueError):
        return
    if value > 0:
        payload["thinking_budget_tokens"] = value


def _fit_reasoning_effort_to_template(payload: Dict, url: str) -> None:
    """A local llama-server renders `reasoning_effort` into the model's chat
    template, which may raise on a value it does not list (HTTP 500)."""
    _mirror_thinking_budget(payload, url)
    effort = payload.get("reasoning_effort")
    if not effort or not _is_self_hosted_openai_compatible(url) or _is_local_ollama_target(url):
        return
    try:
        from src.chat_helpers import llamacpp_reasoning_efforts
        accepted = llamacpp_reasoning_efforts(url)
    except Exception:  # noqa: BLE001 - unknown template, leave the value alone
        return
    fitted = fit_reasoning_effort(str(effort), accepted)
    if fitted != effort:
        logger.info("[reasoning] effort %r is not in this template's %s; sending %r",
                    effort, accepted, fitted)
    if fitted:
        payload["reasoning_effort"] = fitted
    else:
        payload.pop("reasoning_effort", None)


def _apply_gen_overrides_openai(payload: Dict, overrides: Dict, url: str) -> None:
    """Apply pinned sampling params to an OpenAI-compatible payload."""
    for k in ("top_p", "seed", "presence_penalty", "frequency_penalty"):
        if k in overrides:
            payload[k] = overrides[k]
    if "reasoning_effort" in overrides:
        payload["reasoning_effort"] = overrides["reasoning_effort"]
    # Ollama's /v1 surface accepts these Ollama-only knobs at the top level.
    if _is_ollama_openai_compat_url(url):
        if "think" in overrides:
            payload["think"] = overrides["think"]
        for k in ("top_k", "repeat_penalty", "min_p"):
            if k in overrides:
                payload[k] = overrides[k]
    elif _is_loopback_url(url):
        # A non-Ollama local OpenAI-compatible server (llama-server et al.)
        # also accepts these as top-level fields on /v1/chat/completions and
        # a broken sampler there degenerates the same way a fresh Ollama
        # session does. Restricted to loopback: an arbitrary remote OpenAI
        # provider may 400 on an unknown field, and there is no way to know
        # from the URL alone whether it will.
        for k in ("top_k", "repeat_penalty", "min_p"):
            if k in overrides:
                payload[k] = overrides[k]


def _local_sampler_default(setting_key: str, fallback: float) -> float:
    """Read a `local_*_default` sampler setting, falling back safely."""
    try:
        from src.settings import get_setting
        raw = get_setting(setting_key, fallback)
    except Exception:
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _local_sampler_default_optional(setting_key: str) -> Optional[float]:
    """Like `_local_sampler_default`, but for the temperature/top_p/top_k
    floors (`local_temperature_default`/`local_top_p_default`/
    `local_top_k_default`, settings.py) where 0 or empty explicitly means
    "do not send this field" rather than "send zero". Returns ``None`` in
    that case, and whenever the setting is missing or not a number."""
    try:
        from src.settings import get_setting
        raw = get_setting(setting_key, None)
    except Exception:
        return None
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def local_temperature_floor(endpoint_url: str, temperature_explicit: bool) -> Optional[float]:
    """Resolve `local_temperature_default` (settings.py) for one chat/agent
    turn. Returns ``None`` when the caller must keep whatever temperature it
    already has: an explicit per-turn/session override (`temperature_explicit`
    True — a `/temp` in this chat), a non-local endpoint, or the setting
    itself is 0/empty ("do not send"). Called from
    `src.agent_loop._stream_agent_loop_body`, ahead of the existing
    `agent_local_temperature_cap` logic, so a local turn gets the
    measured-good default (0.6) instead of the engine's own (1.0,
    `LLMConfig.DEFAULT_TEMPERATURE`) before any narrower cap is considered.
    """
    if temperature_explicit:
        return None
    try:
        from src.model_context import is_local_endpoint
    except Exception:
        return None
    try:
        if not is_local_endpoint(endpoint_url):
            return None
    except Exception:
        return None
    return _local_sampler_default_optional("local_temperature_default")


def _apply_gen_overrides_ollama(payload: Dict, overrides: Dict) -> None:
    """Apply pinned sampling params to a native Ollama /api/chat payload.

    Also floors `repeat_penalty`/`min_p` to non-degenerate defaults
    (`local_repeat_penalty_default` / `local_min_p_default`, settings.py) when
    neither a saved per-model option nor an explicit per-request override set
    them. Ollama's own defaults (repeat_penalty 1.0, min_p 0) let a fresh
    session decode into a single repeated token — "0000…" — until Ollama
    itself aborted the stream with "prediction aborted, token repeat limit
    reached" (HTTP 400 mid-stream, seen live with a local 27B model). An
    explicit saved or per-request value always wins over this floor.
    """
    options = payload.setdefault("options", {})
    # The per-model `extra` block first, so the named knobs below (a `/ctx`
    # in the chat, a saved num_gpu) still win over it.
    extra = overrides.get("extra")
    if isinstance(extra, dict):
        options.update(extra)
    for k in ("top_p", "top_k", "min_p", "seed", "num_ctx", "num_gpu", "main_gpu", "repeat_penalty", "presence_penalty", "frequency_penalty"):
        if k in overrides:
            options[k] = overrides[k]
    if "think" in overrides:
        payload["think"] = overrides["think"]
    if "keep_alive" in overrides:
        payload["keep_alive"] = overrides["keep_alive"]
    if "repeat_penalty" not in options:
        options["repeat_penalty"] = _local_sampler_default("local_repeat_penalty_default", 1.05)
    if "min_p" not in options:
        options["min_p"] = _local_sampler_default("local_min_p_default", 0.05)


def _ollama_native_url_for_compat(url: str) -> str:
    """http://host:11434/v1[/...] → http://host:11434/api/chat (same server)."""
    parsed = urlparse((url or "").strip())
    root = f"{parsed.scheme or 'http'}://{parsed.netloc}"
    return root.rstrip("/") + "/api/chat"


_ollama_caps_cache: Dict[Tuple[str, str], Tuple[float, Optional[frozenset]]] = {}
_OLLAMA_CAPS_TTL = 600.0


def _ollama_model_caps(url: str, model: str) -> Optional[frozenset]:
    """Capabilities Ollama reports for `model` (/api/show → ["completion",
    "tools", "thinking", …]), cached 10 min. None when unknown (server down,
    model missing) — callers must then leave the request as it is."""
    try:
        parsed = urlparse((url or "").strip())
        base = f"{parsed.scheme or 'http'}://{parsed.netloc}"
    except ValueError:
        return None
    key = (base, model)
    now = time.time()
    hit = _ollama_caps_cache.get(key)
    if hit and now - hit[0] < _OLLAMA_CAPS_TTL:
        return hit[1]
    caps: Optional[frozenset] = None
    try:
        r = httpx.post(base + "/api/show", json={"model": model}, timeout=3.0)
        if r.status_code < 400:
            data = r.json() or {}
            caps = frozenset(str(c) for c in (data.get("capabilities") or []))
    except Exception as e:  # noqa: BLE001 — best effort
        logger.debug("ollama /api/show failed for %s: %s", model, e)
    _ollama_caps_cache[key] = (now, caps)
    return caps


def _route_for_gen_overrides(url: str, gen_overrides: Optional[Dict], model: str = "") -> str:
    """Ollama's OpenAI-compatible /v1 surface ignores the top-level `think`
    flag (verified on Ollama 0.33.2: think=false still streamed reasoning), so
    a thinking toggle is only honoured by the native /api/chat endpoint: a
    pinned one (/think off|on, the harness' runaway-thinking retry) and the
    default suppression for thinking-capable models that the /v1 branch below
    tries to apply with `think: false`. Same server, same model, same tools;
    only the wire format changes."""
    try:
        parsed = urlparse(url or "")
        port = parsed.port
    except ValueError:
        return url
    path = (parsed.path or "").rstrip("/")
    if not (path == "/v1" or path.startswith("/v1/")):
        return url
    # Only the default Ollama port: a llama.cpp / vLLM server on another local
    # port also matches _is_ollama_openai_compat_url and has no /api/chat…
    # unless the admin declared that server as Ollama by saving load options
    # for it (Settings → Local models lists any "ollama" host, port aside):
    # those saved num_ctx/num_gpu/keep_alive were resolved, merged and then
    # silently dropped here because the /v1 surface cannot carry them.
    if not ((port == 11434 and _is_ollama_openai_compat_url(url))
            or _is_declared_ollama_host(url)
            or (model and _model_load_defaults(url, model))):
        return url
    ov = gen_overrides if isinstance(gen_overrides, dict) else {}
    # top_k / repeat_penalty / num_ctx / keep_alive are Ollama `options` with
    # no OpenAI equivalent either — only the native payload can carry them.
    if ov.get("think") is not None or any(k in ov for k in _OLLAMA_NATIVE_ONLY_KEYS):
        return _ollama_native_url_for_compat(url)
    # Default suppression: only for models Ollama itself reports as thinking-
    # capable. A name match alone (qwen3-coder matches "qwen3") must not move
    # a non-thinking model off the wire format it was configured with.
    if model and _supports_thinking(model):
        caps = _ollama_model_caps(url, model)
        if caps is not None and "thinking" in caps:
            return _ollama_native_url_for_compat(url)
    return url


def _stream_error_chunk(status: int, message: str, *, error_class: str,
                         attempts: int, retryable: bool,
                         partial: bool = False,
                         fallback_eligible: Optional[bool] = None) -> str:
    """Typed `event: error` SSE chunk for a streaming transport failure
    (CALL-06, spec §34.5), used by the retry loops in `_stream_llm_inner`.

    Carries the same `error_class`/`attempts`/`retryable` fields the
    non-streaming loop attaches to its raised HTTPException (see
    `llm_call_async`'s local `_annotate`), so a caller already reading those
    off a non-streaming failure finds the same vocabulary here.

    `partial=True` marks a cut that happened AFTER at least one delta was
    already forwarded to the client this attempt: some of the answer is
    already on screen, so this is `outcome_unknown` in the streaming sense
    — this loop itself never retries such a cut (that would resend the
    prompt and duplicate the visible text), and the caller must not treat
    the turn as if nothing happened.
    """
    payload = {
        "error": message,
        "status": status,
        "error_class": error_class,
        "attempts": attempts,
        "retryable": retryable,
    }
    if partial:
        payload["partial"] = True
    if fallback_eligible is not None:
        payload["fallback_eligible"] = fallback_eligible
    return f'event: error\ndata: {json.dumps(payload)}\n\n'


def _stream_status_error_chunk(status: int, friendly: str, raw: str, *,
                                error_class: str, attempts: int, retryable: bool) -> str:
    """`event: error` chunk for a non-2xx stream response (CALL-06).

    Keeps the existing status/text/raw shape callers already parse
    (`_stream_error_status`, the frontend's error banner) and only adds the
    §34.5 fields `_stream_error_chunk` also carries. A definite HTTP status
    is never `outcome_unknown` — the request WAS answered, just with an
    error — so there is no `partial` field here.
    """
    payload = {
        "status": status, "text": friendly, "raw": raw[:500],
        "error_class": error_class, "attempts": attempts, "retryable": retryable,
    }
    return f'event: error\ndata: {json.dumps(payload)}\n\n'


EMPTY_COMPLETION_ERROR_CLASS = "generation.empty_completion"


def _empty_completion_error_chunk(message: str) -> str:
    """Terminal SSE error for a stream that finished without text or tools.

    Distinct from a transport 502: the provider answered, just with nothing
    the caller can use. Foreground routing must not switch models on this
    (`fallback_eligible: false`); the agent harness can retry the same route.
    """
    return (
        "event: error\ndata: "
        + json.dumps({
            "error": message,
            "status": 502,
            "error_class": EMPTY_COMPLETION_ERROR_CLASS,
            "fallback_eligible": False,
        })
        + "\n\n"
    )


#: Wording that shows up in the transport exceptions this module wraps into
#: `event: error` chunks (see `_stream_error_chunk` call sites above) when the
#: cut is a lost connection rather than a definite provider response. Matched
#: case-insensitively against `error_data["error"]` by `_looks_like_engine_lost`.
_ENGINE_LOST_WORDING = (
    "connection reset",
    "connection closed",
    "connection refused",
    "cannot reach",
    "server disconnected",
    "remoteprotocolerror",
    "incomplete chunked read",
)
#: HTTP statuses this module uses for a transport failure (connect-phase,
#: protocol/network error, or a timeout) as opposed to a definite response
#: from the provider (e.g. a 400/429 the model itself returned).
_ENGINE_LOST_STATUSES = (502, 503, 504)


def _looks_like_engine_lost(error_data, status: Optional[int] = None) -> bool:
    """True when a stream error reads like the backing engine process died or
    the connection to it was lost mid-call, rather than an ordinary
    provider-side failure (bad request, rate limit, degenerate output, ...).

    Used by the agent harness (`src.agent_loop`) to tell a transport cut —
    the case a managed local engine can recover from by restarting — apart
    from every other `event: error` shape it already handles specially
    (degenerate output, refused images, empty completion): those are checked
    first and never reach here. Matches on either the normalized HTTP status
    (502/503/504, the ones `_stream_error_chunk` uses for connect/protocol/
    network/timeout failures) or wording from the wrapped exception's
    message, since a mid-generation cut can surface under any of those
    statuses depending on which `except` branch in `_stream_llm_inner` caught
    it (`ConnectError` -> 503, `ProtocolError`/`NetworkError` -> 502,
    `PoolTimeout`/`WriteTimeout` -> 504)."""
    if not isinstance(error_data, dict):
        return False
    # A degenerate-output stop is a 502 too, but the engine is fine. Seen
    # live: the second degenerate round of a turn (its retry already spent)
    # fell through to here and the harness "restarted" a healthy 27B.
    if str(error_data.get("error_class") or "") == DEGENERATE_OUTPUT_ERROR_CLASS:
        return False
    if status is None:
        status = error_data.get("status")
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int in _ENGINE_LOST_STATUSES:
        return True
    message = str(error_data.get("error") or "").lower()
    return any(wording in message for wording in _ENGINE_LOST_WORDING)


def is_empty_completion_error(error_data) -> bool:
    """True when a stream error is a clean empty completion, not a transport cut."""
    if not isinstance(error_data, dict):
        return False
    if str(error_data.get("error_class") or "") == EMPTY_COMPLETION_ERROR_CLASS:
        return True
    return "no substantive output" in str(error_data.get("error") or "").lower()


def _stream_refusal_event(text: str = "", *, stop_reason: Optional[str] = None) -> str:
    """Typed SSE event for a provider-issued refusal (MOD-03), instead of
    letting it masquerade as ordinary answer text: OpenAI-compatible's
    `delta.refusal` field carries incremental refusal text directly (`text`);
    Anthropic signals it only via `stop_reason: "refusal"` on the already-
    streamed text, so `stop_reason` is passed through instead. Kept on its
    own `type` so the frontend never renders or animates it as if it were
    the model's normal reply."""
    payload = {"type": "refusal"}
    if text:
        payload["text"] = text
    if stop_reason:
        payload["stop_reason"] = stop_reason
    return f'data: {json.dumps(payload)}\n\n'


def _stream_retry_decision(*, status: Optional[int] = None,
                            exc: Optional[BaseException] = None,
                            headers: Optional[Mapping] = None,
                            attempt: int, max_retries: int,
                            budget: RetryBudget,
                            delta_emitted: bool,
                            fail_fast: bool = False) -> Tuple[bool, float, RetryClass, bool]:
    """Classify one failed streaming attempt and decide what happens next
    (CALL-06, spec §34.5) — the streaming-loop counterpart of the inline
    decisions `llm_call_async` makes for the non-streaming path, returning a
    decision instead of raising/sleeping directly (a generator must `yield`
    its failure, not raise it, and must additionally never resend after
    `delta_emitted`).

    Returns `(should_retry, wait_seconds, classification, retryable)`. A cut
    classified `outcome_unknown` (a read timeout or reset once bytes were
    already exchanged) is retried ONLY when nothing has been forwarded to
    the client yet — once `delta_emitted` is True, retrying would resend the
    prompt and duplicate the text already on screen, so the caller must stop
    and emit a `partial` error instead.

    `fail_fast` mirrors `llm_call_async`'s `availability_only_transport`:
    a caller juggling its OWN fallback chain across candidates (see
    `stream_llm_with_fallback`) wants a single fast, definitive failure per
    candidate rather than this loop burning `max_retries` in place before
    the chain can move on — pass it for exactly the exception classes that
    function gates the same way (pool/write/protocol/network), never for a
    connect-phase failure (already bounded by the dead-host cooldown) or a
    read timeout (already its own outcome_unknown special case).
    """
    classification = classify_http(status=status, headers=headers, exc=exc)
    retryable = classification is not RetryClass.NO_RETRY
    if fail_fast:
        return False, 0.0, classification, False
    if classification is RetryClass.OUTCOME_UNKNOWN and delta_emitted:
        return False, 0.0, classification, retryable
    can_retry = classification in (RetryClass.RETRY_NOW, RetryClass.RETRY_BACKOFF) or (
        classification is RetryClass.OUTCOME_UNKNOWN and not delta_emitted
    )
    if can_retry and attempt < max_retries and not budget.exhausted():
        retry_after = parse_retry_after(headers) if status is not None else None
        return True, _retry_delay(attempt, retry_after=retry_after), classification, retryable
    return False, 0.0, classification, retryable


async def _stream_retry_or_fail(*, should_retry: bool, wait: float,
                                 error_chunk: str, retry_call):
    """Shared tail for every streaming except-clause / non-2xx-status branch
    in `_stream_llm_inner` (CALL-06): either sleep and tail-recurse into the
    next attempt via `retry_call` (a zero-arg callable returning the retried
    attempt's async generator), or yield the already-built terminal
    `error_chunk` (from `_stream_error_chunk`/`_stream_status_error_chunk`).
    """
    if should_retry:
        await asyncio.sleep(wait)
        async for chunk in retry_call():
            yield chunk
        return
    yield error_chunk


async def stream_llm(url: str, model: str, messages: List[Dict], temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
                     max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS, headers: Optional[Dict] = None,
                     timeout: int = LLMConfig.STREAM_TIMEOUT, prompt_type: Optional[str] = None,
                     tools: Optional[List[Dict]] = None, session_id: Optional[str] = None,
                     tool_choice_none: bool = False, workload: str = "foreground",
                     gen_overrides: Optional[Dict] = None, max_retries: int = LLMConfig.MAX_RETRIES,
                     availability_only_transport: bool = False):
    """Traced wrapper (LLM-TRACE-01) around ``_stream_llm_traced_source``.

    Streaming has no single "final response" moment — the text arrives as a
    sequence of chunks over possibly many seconds — so this accumulates every
    chunk AS IT PASSES THROUGH (never buffering the stream itself; each chunk
    is yielded to the real caller immediately) and records one trace entry
    when the generator ends, whether that is a normal finish, an upstream
    error event, an exception, or the caller cancelling the generator (walrus
    the finally so cancellation still records what was seen so far).
    """
    from src import llm_trace
    if not llm_trace.tracing_enabled() or not session_id:
        async for chunk in _stream_llm_traced_source(
            url, model, messages, temperature=temperature, max_tokens=max_tokens,
            headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
            session_id=session_id, tool_choice_none=tool_choice_none, workload=workload,
            gen_overrides=gen_overrides, max_retries=max_retries,
            availability_only_transport=availability_only_transport,
        ):
            yield chunk
        return

    _t0 = time.time()
    acc = llm_trace.StreamAccumulator()
    _exc_text: Optional[str] = None
    try:
        async for chunk in _stream_llm_traced_source(
            url, model, messages, temperature=temperature, max_tokens=max_tokens,
            headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
            session_id=session_id, tool_choice_none=tool_choice_none, workload=workload,
            gen_overrides=gen_overrides, max_retries=max_retries,
            availability_only_transport=availability_only_transport,
        ):
            try:
                acc.feed(chunk)
            except Exception:
                pass
            yield chunk
    except Exception as exc:
        _exc_text = str(exc)
        raise
    finally:
        try:
            llm_trace.record_call(
                session_id=session_id,
                endpoint_url=url,
                model=model,
                request={
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "tools": tools,
                    "tool_choice_none": tool_choice_none,
                    "gen_overrides": gen_overrides,
                    "headers": headers,
                },
                response_text=acc.text,
                thinking_text=acc.thinking,
                tool_calls=acc.tool_calls,
                finish_reason=acc.finish_reason,
                usage=acc.usage,
                duration_ms=(time.time() - _t0) * 1000.0,
                error=_exc_text or acc.error,
            )
        except Exception:
            logger.debug("[llm_trace] streaming record failed", exc_info=True)


async def _stream_llm_traced_source(url: str, model: str, messages: List[Dict], temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
                     max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS, headers: Optional[Dict] = None,
                     timeout: int = LLMConfig.STREAM_TIMEOUT, prompt_type: Optional[str] = None,
                     tools: Optional[List[Dict]] = None, session_id: Optional[str] = None,
                     tool_choice_none: bool = False, workload: str = "foreground",
                     gen_overrides: Optional[Dict] = None, max_retries: int = LLMConfig.MAX_RETRIES,
                     availability_only_transport: bool = False):
    if str(url or '').startswith('faustus-cli://'):
        from src.cli_model import stream
        async for chunk in stream(url, model, messages, headers, timeout, tools=tools):
            yield chunk
        return
    # Saved per-model load defaults (Settings → Local models) ride along as
    # overrides — under the caller's own — so a saved num_ctx also triggers
    # the native reroute below and lands in the Ollama `options`.
    gen_overrides = _with_model_defaults(url, model, gen_overrides)
    _routed = _route_for_gen_overrides(url, gen_overrides, model)
    if _routed != url:
        caps = _ollama_model_caps(url, model)
        gen_overrides = dict(gen_overrides or {})
        if caps is not None and "thinking" not in caps:
            # Ollama rejects `think` for models without the capability.
            gen_overrides.pop("think", None)
        elif gen_overrides.get("think") is None and _supports_thinking(model):
            # Rerouted for the default suppression: make it explicit so the
            # native payload carries think=false.
            gen_overrides["think"] = False
        logger.info("Ollama /v1 -> native /api/chat for %s (think=%s)", model, gen_overrides.get("think"))
        url = _routed
    target_url = _stream_target_url(url)
    async with _local_model_slot(target_url, model, workload):
        async for chunk in _stream_llm_inner(
            url,
            model,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            headers=headers,
            timeout=timeout,
            prompt_type=prompt_type,
            tools=tools,
            session_id=session_id,
            tool_choice_none=tool_choice_none,
            gen_overrides=gen_overrides,
            max_retries=max_retries,
            availability_only_transport=availability_only_transport,
        ):
            yield chunk


async def _stream_llm_inner(url: str, model: str, messages: List[Dict], temperature: float = LLMConfig.DEFAULT_TEMPERATURE,
                            max_tokens: int = LLMConfig.DEFAULT_MAX_TOKENS, headers: Optional[Dict] = None,
                            timeout: int = LLMConfig.STREAM_TIMEOUT, prompt_type: Optional[str] = None,
                            tools: Optional[List[Dict]] = None, session_id: Optional[str] = None,
                            tool_choice_none: bool = False, gen_overrides: Optional[Dict] = None,
                            max_retries: int = LLMConfig.MAX_RETRIES,
                            availability_only_transport: bool = False,
                            _attempt: int = 1, _budget: Optional[RetryBudget] = None,
                            _engine_wait_used: bool = False):
    _overrides = _clean_gen_overrides(gen_overrides)
    """Stream LLM responses with improved error handling.

    Yields SSE chunks:
      - data: {"delta": "text"}           — text content
      - data: {"type": "tool_calls", ...}  — accumulated native tool calls (before DONE)
      - data: {"type": "refusal", ...}     — provider refusal (MOD-03), never disguised as text
      - event: error                       — errors
      - data: [DONE]                       — end of stream

    Retries (CALL-06, spec §34.5) use the same `src.retry_policy` classes as
    `llm_call_async`: 429/503 honour Retry-After, other 5xx and connect-phase
    transport errors use jittered backoff, 4xx never retries. Each provider
    branch below retries by tail-recursing into a fresh attempt (`_attempt`/
    `_budget` are internal — never pass them from outside) rather than
    looping in place, so none of the SSE parsing logic has to be reindented.
    A cut that happens AFTER at least one delta was already yielded to the
    caller this attempt is `outcome_unknown` in the streaming sense and is
    NEVER retried here (that would resend the prompt and duplicate the text
    already on screen): the branch instead yields a typed error with
    `partial: true` and stops. A cut before any delta was sent behaves like
    the non-streaming loop and retries transparently.

    `availability_only_transport` mirrors `llm_call_async`'s parameter of
    the same name: pass it when the CALLER already runs its own fallback
    chain across candidates (`stream_llm_with_fallback`) so a pool/write/
    protocol/network failure fails fast (one attempt, non-retryable) instead
    of burning `max_retries` in place before the chain can move to the next
    candidate. Connect-phase failures and read timeouts are unaffected —
    they already have their own fast-fail mechanisms (dead-host cooldown,
    outcome_unknown).
    """
    if _budget is None:
        _budget = RetryBudget(LLMConfig.RETRY_TIME_BUDGET)
        _budget.start()
    provider = _detect_provider(url)
    messages_copy = _sanitize_llm_messages(messages, provider=provider)

    # Consolidate multiple system messages into one at the start.
    # Some models (e.g. Qwen3.5) reject system messages that aren't first.
    sys_parts = []
    non_sys = []
    for m in messages_copy:
        if m.get("role") == "system":
            sys_parts.append(m.get('content') or '')
        else:
            non_sys.append(m)
    if sys_parts:
        messages_copy = [{"role": "system", "content": "\n\n".join(sys_parts)}] + non_sys
    else:
        messages_copy = non_sys

    if provider == "anthropic":
        target_url = _normalize_anthropic_url(url)
        h = _build_anthropic_headers(headers)
        payload = _build_anthropic_payload(model, messages_copy, temperature, max_tokens, stream=True, tools=tools)
    elif provider == "ollama":
        target_url = _normalize_ollama_url(url)
        h = {"Content-Type": "application/json"}
        if headers:
            h.update(headers)
        payload = _build_ollama_payload(
            model, messages_copy, temperature, max_tokens,
            stream=True, tools=tools, num_ctx=get_context_length(url, model),
        )
        if _overrides:
            _apply_gen_overrides_ollama(payload, _overrides)
    elif provider == "chatgpt-subscription":
        target_url = _normalize_chatgpt_subscription_url(url)
        h = _provider_headers(provider, headers)
        payload = _build_chatgpt_responses_payload(model, messages_copy, temperature, max_tokens, stream=True)
    else:
        target_url = _normalize_openai_chat_url(url)
        payload = {
            "model": model,
            "messages": messages_copy,
            "temperature": temperature,
            "stream": True,
        }
        if _omit_temperature(provider, model):
            payload.pop("temperature", None)
        if provider not in {"openrouter", "groq"}:
            payload["stream_options"] = {"include_usage": True}
        if max_tokens and max_tokens > 0:
            tok_key = "max_completion_tokens" if _uses_max_completion_tokens(model) else "max_tokens"
            payload[tok_key] = max_tokens
        if tools:
            payload["tools"] = _alias_harmony_tools(tools, model)
        elif tool_choice_none:
            payload["tool_choice"] = "none"
        # Mistral thinking-capable models — send reasoning_effort so Mistral
        # activates thinking mode and returns structured reasoning_content.
        # Effort level is configurable via ODYSSEUS_MISTRAL_REASONING_EFFORT
        # (high / medium / low / none); default "high".
        if provider == "mistral" and _supports_thinking(model):
            payload["reasoning_effort"] = _MISTRAL_REASONING_EFFORT
        # For Ollama's OpenAI-compat /v1 endpoint with thinking models (qwen3,
        # gemma4, etc.), suppress thinking so tool calls aren't swallowed inside
        # <think> blocks. Ollama /v1 accepts "think": false as a top-level param.
        if _is_ollama_openai_compat_url(url) and _supports_thinking(model):
            payload["think"] = False
        # A self-hosted, non-Ollama OpenAI-compatible endpoint (llama-server,
        # vLLM) needs the SAME think decision Ollama gets above, carried
        # through a different field: llama.cpp's --jinja Qwen3 template opens
        # every assistant turn with <think> unconditionally, so without this
        # a turn burns the whole output cap reasoning every round. Verified
        # live: 4 rounds, 40 minutes, no answer against llama-server; the
        # Ollama native path (think=False reaching it) finished the same
        # turn in 158s. `chat_template_kwargs.enable_thinking` is llama.cpp/
        # vLLM's equivalent; `reasoning_budget` (llama.cpp, recent builds)
        # caps how much of the output cap thinking itself can spend, only
        # sent when thinking is actually on.
        if _is_self_hosted_openai_compatible(url) and not _is_local_ollama_target(url):
            _think_decision = _resolve_think_decision(model, _overrides)
            if _think_decision is not None:
                _ctk = payload.get("chat_template_kwargs")
                if not isinstance(_ctk, dict):
                    _ctk = {}
                    payload["chat_template_kwargs"] = _ctk
                _ctk["enable_thinking"] = _think_decision
                if _think_decision:
                    # An explicit per-request budget (e.g. delegate_agents'
                    # `effort: "high"`, src/effort_profile.py) wins over the
                    # saved setting default, the same way `think` itself
                    # already does above.
                    if "reasoning_budget" in _overrides:
                        try:
                            _budget = int(_overrides["reasoning_budget"])
                        except (TypeError, ValueError):
                            _budget = 4096
                    else:
                        try:
                            _budget = int(_local_sampler_default("local_openai_reasoning_budget_default", 4096))
                        except (TypeError, ValueError):
                            _budget = 4096
                    if _budget > 0:
                        payload["reasoning_budget"] = _budget
        if _overrides:
            _apply_gen_overrides_openai(payload, _overrides, url)
        _fit_reasoning_effort_to_template(payload, url)
        _apply_local_cache_affinity(payload, url, session_id)
        _apply_local_generation_stability(payload, target_url, model)
        _scrub_openai_chat_tool_reasoning(payload, target_url, model)
        # The streaming path is the one the user is watching, and agent mode
        # is the default here, so this is where a greeting arrives with the
        # whole toolset attached. `turn_effort` decides; a conversation that
        # has already run a tool always keeps its reasoning.
        _suppress_thinking_for_small_talk(payload, model, messages_copy, tools)
        _drop_tools_for_small_talk(payload, messages_copy)
        if provider == "openrouter":
            # Same OpenRouter options application as llm_call (OBJ-8 Lote A2)
            # -- see that call site for the full rationale. `tools` is known
            # here, so the anthropic/* cache breakpoint also covers the
            # tool-call agentic case, not just a long system prompt.
            try:
                from src.openrouter_options import apply_openrouter_payload
                apply_openrouter_payload(payload, provider=provider, endpoint_id=_endpoint_id_for_url(url), model=model)
            except Exception as exc:  # noqa: BLE001
                logger.debug("openrouter_options: apply_openrouter_payload failed for %s: %s", model, exc)
            if _openrouter_anthropic_cache_hints_applicable(provider, model):
                _apply_openrouter_anthropic_cache_hints(payload, tools=tools)
        h = _provider_headers(provider, headers)
        if provider == "copilot":
            from src.copilot import apply_request_headers
            apply_request_headers(h, messages_copy)

    # Connect budget from LLMConfig.CONNECT_TIMEOUT (env LLM_CONNECT_TIMEOUT).
    # The dead-host cooldown still bounds a genuinely unreachable upstream, so a
    # wider connect budget only affects first contact and stops a brief cold
    # connect blip (offshore/public endpoints) surfacing as a 503 on this stream
    # path, which -- unlike llm_call -- does not retry the connect.
    stream_timeout = _stream_timeout(timeout)

    if _is_host_dead(target_url):
        yield f'event: error\ndata: {json.dumps({"error": f"Upstream {_host_key(target_url)} unreachable (cooldown active)", "status": 503})}\n\n'
        return
    note_model_activity(target_url, model)
    degenerate_guard = _DegenerateStreamGuard(model)

    # ── ChatGPT Subscription / Codex Responses streaming ──
    if provider == "chatgpt-subscription":
        event_name = ""
        input_tokens = 0
        output_tokens = 0
        _responses_actual_model = ""
        _responses_model_announced = False
        _delta_emitted = False

        def _retry(next_attempt: int):
            return _stream_llm_inner(
                url, model, messages, temperature=temperature, max_tokens=max_tokens,
                headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
                session_id=session_id, tool_choice_none=tool_choice_none,
                gen_overrides=gen_overrides, max_retries=max_retries,
                availability_only_transport=availability_only_transport,
                _attempt=next_attempt, _budget=_budget,
            )
        try:
            client = _get_http_client()
            async with client.stream('POST', target_url, json=payload, headers=h, timeout=stream_timeout) as r:
                _clear_host_dead(target_url)
                if r.status_code != 200:
                    raw = (await r.aread()).decode(errors="replace")
                    friendly = _format_chatgpt_subscription_error(r.status_code, raw)
                    _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                        status=r.status_code, headers=r.headers, attempt=_attempt,
                        max_retries=max_retries, budget=_budget, delta_emitted=False,
                    )
                    if _should_retry and parse_retry_after(r.headers) is not None:
                        logger.info(
                            f"ChatGPT Subscription stream to {target_url}: server busy "
                            f"(HTTP {r.status_code}), retrying in {_wait:.1f}s (Retry-After honoured)"
                        )
                    async for chunk in _stream_retry_or_fail(
                        should_retry=_should_retry, wait=_wait,
                        error_chunk=_stream_status_error_chunk(
                            r.status_code, friendly, raw,
                            error_class=_retry_error_class(status=r.status_code),
                            attempts=_attempt,
                            retryable=_retryable,
                        ),
                        retry_call=lambda: _retry(_attempt + 1),
                    ):
                        yield chunk
                    return
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    evt = data.get("type") or event_name
                    response_data = data.get("response") or {}
                    reported_model = (
                        response_data.get("model")
                        if isinstance(response_data, dict)
                        else None
                    )
                    reported_model = _reported_model_name(
                        reported_model or data.get("model")
                    )
                    if reported_model:
                        _responses_actual_model = reported_model
                        if not _responses_model_announced:
                            model_event = _model_actual_event(model, reported_model)
                            if model_event:
                                _responses_model_announced = True
                                yield model_event
                    if evt == "response.output_text.delta":
                        delta = data.get("delta") or ""
                        if delta:
                            try:
                                degenerate_guard.check(delta)
                            except DegenerateOutput as _degenerate:
                                yield _degenerate_output_error_chunk(_degenerate)
                                return
                            _delta_emitted = True
                            yield f'data: {json.dumps({"delta": delta})}\n\n'
                    elif evt == "response.completed":
                        usage = (data.get("response") or {}).get("usage") or data.get("usage") or {}
                        if isinstance(usage, dict):
                            raw_input = (
                                usage.get("input_tokens")
                                if "input_tokens" in usage
                                else usage.get("prompt_tokens", input_tokens)
                            )
                            raw_output = (
                                usage.get("output_tokens")
                                if "output_tokens" in usage
                                else usage.get("completion_tokens", output_tokens)
                            )
                            normalized_usage = _normalize_usage_counts(
                                raw_input,
                                raw_output,
                            )
                            if normalized_usage and (
                                "input_tokens" in usage
                                or "prompt_tokens" in usage
                                or "output_tokens" in usage
                                or "completion_tokens" in usage
                            ):
                                normalized_usage.update(_extract_usage_extras(usage))
                                _annotate_usage_model(
                                    normalized_usage,
                                    model,
                                    _responses_actual_model,
                                )
                                yield f'data: {json.dumps({"type": "usage", "data": normalized_usage})}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                    elif evt in ("response.failed", "error"):
                        err = data.get("error") or (data.get("response") or {}).get("error") or {}
                        if evt == "error" and not err:
                            # Responses API ``error`` events carry code/message
                            # at the top level, unlike ``response.failed``.
                            err = {
                                key: data[key]
                                for key in ("type", "code", "message", "status", "status_code", "http_status")
                                if key in data
                            }
                        text = err.get("message") if isinstance(err, dict) else str(err or "ChatGPT Subscription request failed")
                        status = _provider_stream_error_status(err, default=400)
                        yield f'event: error\ndata: {json.dumps({"status": status, "text": text})}\n\n'
                        return
                yield "data: [DONE]\n\n"
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _cooled = _mark_host_dead(target_url)
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
            )
            _should_retry = _should_retry and not _cooled
            _tail = f" — host cooled for {DEAD_HOST_COOLDOWN:.0f}s" if _cooled else " — transient, will retry"
            logger.warning(f"ChatGPT Subscription stream connect to {target_url} failed: {e}{_tail}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    503, f"Cannot reach {_host_key(target_url)}: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.ReadTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
            )
            logger.warning(
                f"ChatGPT Subscription stream read timed out (attempt {_attempt}): {e} "
                f"— the request may have completed upstream even though no full response arrived"
            )
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504,
                    "Read timeout after partial output; outcome unknown"
                    if _delta_emitted else
                    f"Read timeout after {_attempt} attempt(s); outcome unknown",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=False,  # outcome_unknown: never auto-retryable for an external caller (§34.5)
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.PoolTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"ChatGPT Subscription stream connection pool timed out (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504, f"Connection pool timeout after {_attempt} attempt(s)",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.WriteTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"ChatGPT Subscription stream upstream timeout (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504, f"Upstream timeout after {_attempt} attempt(s)",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.ProtocolError as e:
            # RemoteProtocolError (server reset/violated protocol mid-stream)
            # classifies outcome_unknown; a local one classifies retry_backoff
            # — see retry_policy.classify_http.
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"ChatGPT Subscription stream protocol failure (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    502, f"Upstream protocol error: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.NetworkError as e:
            # ReadError (reset while reading the response) classifies
            # outcome_unknown; WriteError/CloseError classify retry_backoff.
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"ChatGPT Subscription stream network failure (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    502, f"Network error: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except Exception as e:
            # Not a recognised transport failure (e.g. a bug in the parsing
            # above): never retried, same as before this module.
            logger.error(f"ChatGPT Subscription stream error: {e}")
            yield _stream_error_chunk(
                502, str(e), error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=False, partial=_delta_emitted, fallback_eligible=False,
            )
        return

    # ── Native Ollama streaming ──
    if provider == "ollama":
        _ollama_tool_calls: List[Dict] = []
        _harmony_router = _HarmonyStreamRouter()
        _ollama_actual_model = ""
        _ollama_model_announced = False
        _delta_emitted = False

        def _retry(next_attempt: int):
            return _stream_llm_inner(
                url, model, messages, temperature=temperature, max_tokens=max_tokens,
                headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
                session_id=session_id, tool_choice_none=tool_choice_none,
                gen_overrides=gen_overrides, max_retries=max_retries,
                availability_only_transport=availability_only_transport,
                _attempt=next_attempt, _budget=_budget,
            )
        try:
            client = _get_http_client()
            async with client.stream('POST', target_url, json=payload, headers=h, timeout=stream_timeout) as r:
                _clear_host_dead(target_url)
                if r.status_code != 200:
                    raw = (await r.aread()).decode(errors="replace")
                    friendly = _format_upstream_error(r.status_code, raw, target_url)
                    _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                        status=r.status_code, headers=r.headers, attempt=_attempt,
                        max_retries=max_retries, budget=_budget, delta_emitted=False,
                    )
                    if _should_retry and parse_retry_after(r.headers) is not None:
                        logger.info(
                            f"Ollama stream to {target_url}: server busy (HTTP {r.status_code}), "
                            f"retrying in {_wait:.1f}s (Retry-After honoured)"
                        )
                    async for chunk in _stream_retry_or_fail(
                        should_retry=_should_retry, wait=_wait,
                        error_chunk=_stream_status_error_chunk(
                            r.status_code, friendly, raw,
                            error_class=_retry_error_class(status=r.status_code),
                            attempts=_attempt,
                            retryable=_retryable,
                        ),
                        retry_call=lambda: _retry(_attempt + 1),
                    ):
                        yield chunk
                    return
                async for line in r.aiter_lines():
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if j.get("error"):
                        err = j.get("error")
                        status = _provider_stream_error_status(err, default=400)
                        text = err.get("message") if isinstance(err, dict) else str(err)
                        error_payload = {"error": text or "Ollama request failed", "status": status}
                        # Ollama's own guard for the same failure mode this runtime's
                        # client-side DegenerateOutput guard targets: tag it the same
                        # way so is_degenerate_output_error()/the agent harness treat
                        # both alike (client-side abort is the common case; this is
                        # the fallback for whatever slips past it).
                        if "token repeat limit" in str(text or "").lower():
                            error_payload["error_class"] = DEGENERATE_OUTPUT_ERROR_CLASS
                        yield f'event: error\ndata: {json.dumps(error_payload)}\n\n'
                        return
                    reported_model = _reported_model_name(j.get("model"))
                    if reported_model:
                        _ollama_actual_model = reported_model
                        if not _ollama_model_announced:
                            model_event = _model_actual_event(model, reported_model)
                            if model_event:
                                _ollama_model_announced = True
                                yield model_event
                    message = j.get("message") or {}
                    thinking = message.get("thinking") or ""
                    if thinking:
                        try:
                            degenerate_guard.check_reasoning(thinking)
                        except DegenerateOutput as _degenerate:
                            yield _degenerate_output_error_chunk(_degenerate)
                            return
                        _delta_emitted = True
                        yield _stream_delta_event(thinking, thinking=True)
                    content = message.get("content") or ""
                    if content:
                        try:
                            degenerate_guard.check(content)
                        except DegenerateOutput as _degenerate:
                            yield _degenerate_output_error_chunk(_degenerate)
                            return
                        for part, is_thinking in _harmony_router.feed(content):
                            _delta_emitted = True
                            yield _stream_delta_event(part, thinking=is_thinking)
                    for tc in message.get("tool_calls") or []:
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            # Only accumulated locally — Ollama emits tool_calls as one
                            # block at `done`, so nothing has reached the client yet;
                            # this must NOT set _delta_emitted (that would needlessly
                            # block a safe retry on a cut before `done`).
                            _ollama_tool_calls.append({
                                "id": tc.get("id") or f"call_{len(_ollama_tool_calls)}",
                                "name": _unalias_harmony_tool_name(fn.get("name") or "", model),
                                "arguments": json.dumps(fn.get("arguments") or {}),
                            })
                    if j.get("done"):
                        for part, is_thinking in _harmony_router.flush():
                            yield _stream_delta_event(part, thinking=is_thinking)
                        if _ollama_tool_calls:
                            yield f'data: {json.dumps({"type": "tool_calls", "calls": _ollama_tool_calls})}\n\n'
                        if j.get("prompt_eval_count") is not None or j.get("eval_count") is not None:
                            normalized_usage = _normalize_usage_counts(
                                j.get("prompt_eval_count", 0),
                                j.get("eval_count", 0),
                            )
                            if normalized_usage:
                                # OpenRouter cost/cache/reasoning extras never appear on
                                # a native Ollama `done` message (no `usage.cost` shape
                                # here) — this call is a no-op today, kept only so every
                                # normalized_usage site applies the same helper.
                                normalized_usage.update(_extract_usage_extras(j))
                                # Ollama's own timings, in nanoseconds. This is the
                                # same pure-decode figure llama.cpp reports as
                                # predicted_per_second, and it was being thrown
                                # away: without it the UI falls back to
                                # tokens/wall-clock, which over an agent turn also
                                # divides by the prefill and the tool time. Seen
                                # live (07-09-2026): a model decoding at ~50 t/s
                                # displayed as 0.7 t/s over three rounds.
                                _gen_tps = _ollama_rate(j.get("eval_count"), j.get("eval_duration"))
                                if _gen_tps:
                                    normalized_usage["gen_tps"] = _gen_tps
                                    remember_local_speed(model, j.get("eval_count"), j.get("eval_duration"))
                                _pre_tps = _ollama_rate(
                                    j.get("prompt_eval_count"), j.get("prompt_eval_duration")
                                )
                                if _pre_tps:
                                    normalized_usage["prefill_tps"] = _pre_tps
                                # INF-03: the phase breakdown a "why did it
                                # take this long" view needs — load/prefill/
                                # generation in ms, not just the tok/s two
                                # ratios above already threw away the
                                # numerator/denominator for. Always present
                                # once Ollama sent counters at all; any one
                                # field Ollama omitted stays `None`.
                                normalized_usage["engine_timings"] = _ollama_engine_timings(j)
                                _annotate_usage_model(
                                    normalized_usage,
                                    model,
                                    _ollama_actual_model,
                                )
                                yield f'data: {json.dumps({"type": "usage", "data": normalized_usage})}\n\n'
                        # Ollama reports why it stopped as done_reason
                        # ("stop" | "length" | "load"); normalize to the
                        # OpenAI vocabulary so the agent loop has one signal.
                        _done_reason = j.get("done_reason")
                        if _ollama_tool_calls and _done_reason in (None, "stop"):
                            _done_reason = "tool_calls"
                        yield f'data: {json.dumps({"type": "finish", "finish_reason": _done_reason or "stop"})}\n\n'
                        yield "data: [DONE]\n\n"
                        return
                for part, is_thinking in _harmony_router.flush():
                    yield _stream_delta_event(part, thinking=is_thinking)
                yield "data: [DONE]\n\n"
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _cooled = _mark_host_dead(target_url)
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
            )
            _should_retry = _should_retry and not _cooled
            _tail = f" — host cooled for {DEAD_HOST_COOLDOWN:.0f}s" if _cooled else " — transient, will retry"
            logger.warning(f"Ollama stream connect to {target_url} failed: {e}{_tail}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    503, f"Cannot reach {_host_key(target_url)}: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.ReadTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
            )
            logger.warning(
                f"Ollama stream read timed out (attempt {_attempt}): {e} "
                f"— the request may have completed upstream even though no full response arrived"
            )
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504,
                    "Read timeout after partial output; outcome unknown"
                    if _delta_emitted else
                    f"Read timeout after {_attempt} attempt(s); outcome unknown",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=False,  # outcome_unknown: never auto-retryable for an external caller (§34.5)
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.PoolTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Ollama stream connection pool timed out (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504, f"Connection pool timeout after {_attempt} attempt(s)",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.WriteTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Ollama stream upstream timeout (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504, f"Upstream timeout after {_attempt} attempt(s)",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.ProtocolError as e:
            # RemoteProtocolError (server reset/violated protocol mid-stream)
            # classifies outcome_unknown; a local one classifies retry_backoff
            # — see retry_policy.classify_http.
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Ollama stream protocol failure (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    502, f"Upstream protocol error: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.NetworkError as e:
            # ReadError (reset while reading the response) classifies
            # outcome_unknown; WriteError/CloseError classify retry_backoff.
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Ollama stream network failure (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    502, f"Network error: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except Exception as e:
            # Not a recognised transport failure (e.g. a bug in the parsing
            # above): never retried, same as before this module.
            logger.error(f"Ollama stream error: {e}")
            yield _stream_error_chunk(
                502, str(e), error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=False, partial=_delta_emitted, fallback_eligible=False,
            )
        return

    # ── Anthropic streaming ──
    if provider == "anthropic":
        _anth_input_tokens = 0
        _anth_output_tokens = 0
        _anth_usage_seen = False
        _anth_actual_model = ""
        _anth_model_announced = False
        # Track tool_use blocks: {index: {id, name, arguments_json}}
        _anth_tool_blocks: Dict[int, Dict] = {}
        _anth_block_idx = -1
        _anth_block_type = ""
        _anth_stop_reason = None
        _delta_emitted = False

        def _retry(next_attempt: int):
            return _stream_llm_inner(
                url, model, messages, temperature=temperature, max_tokens=max_tokens,
                headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
                session_id=session_id, tool_choice_none=tool_choice_none,
                gen_overrides=gen_overrides, max_retries=max_retries,
                availability_only_transport=availability_only_transport,
                _attempt=next_attempt, _budget=_budget,
            )
        try:
            client = _get_http_client()
            async with client.stream('POST', target_url, json=payload, headers=h, timeout=stream_timeout) as r:
                _clear_host_dead(target_url)
                if r.status_code != 200:
                    raw = (await r.aread()).decode(errors="replace")
                    friendly = _format_upstream_error(r.status_code, raw, target_url)
                    _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                        status=r.status_code, headers=r.headers, attempt=_attempt,
                        max_retries=max_retries, budget=_budget, delta_emitted=False,
                    )
                    if _should_retry and parse_retry_after(r.headers) is not None:
                        logger.info(
                            f"Anthropic stream to {target_url}: server busy (HTTP {r.status_code}), "
                            f"retrying in {_wait:.1f}s (Retry-After honoured)"
                        )
                    async for chunk in _stream_retry_or_fail(
                        should_retry=_should_retry, wait=_wait,
                        error_chunk=_stream_status_error_chunk(
                            r.status_code, friendly, raw,
                            error_class=_retry_error_class(status=r.status_code),
                            attempts=_attempt,
                            retryable=_retryable,
                        ),
                        retry_call=lambda: _retry(_attempt + 1),
                    ):
                        yield chunk
                    return
                async for line in r.aiter_lines():
                    # SSE allows "data:value" with no space after the colon
                    # (the space is optional per the spec). Some gateways and
                    # local servers omit it; gating on "data: " dropped their
                    # entire stream.
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or not data.startswith("{"):
                        continue
                    try:
                        j = json.loads(data)
                        evt = j.get("type", "")
                        if evt == "content_block_start":
                            _anth_block_idx = j.get("index", _anth_block_idx + 1)
                            cb = j.get("content_block") or {}
                            _anth_block_type = cb.get("type", "text")
                            if _anth_block_type == "tool_use":
                                _anth_tool_blocks[_anth_block_idx] = {
                                    "id": cb.get("id") or f"call_{_anth_block_idx}",
                                    "name": cb.get("name") or "",
                                    "arguments": "",
                                }
                        elif evt == "content_block_delta":
                            delta = j.get("delta") or {}
                            delta_type = delta.get("type", "")
                            if delta_type == "text_delta":
                                text = delta.get("text") or ""
                                if text:
                                    _delta_emitted = True
                                    yield f'data: {json.dumps({"delta": text})}\n\n'
                            elif delta_type == "input_json_delta":
                                # Accumulate tool arguments JSON
                                idx = j.get("index", _anth_block_idx)
                                if idx in _anth_tool_blocks:
                                    partial = delta.get("partial_json") or ""
                                    _anth_tool_blocks[idx]["arguments"] += partial
                                    # Stream tool arg deltas for doc tools
                                    if partial and _anth_tool_blocks[idx].get("name") in ("create_document", "update_document", "edit_document"):
                                        _delta_emitted = True
                                        yield f'data: {json.dumps({"type": "tool_call_delta", "index": idx, "name": _anth_tool_blocks[idx]["name"], "arg_delta": partial})}\n\n'
                        elif evt == "message_start":
                            message_data = j.get("message") or {}
                            reported_model = _reported_model_name(
                                message_data.get("model")
                                if isinstance(message_data, dict)
                                else None
                            )
                            if reported_model:
                                _anth_actual_model = reported_model
                                if not _anth_model_announced:
                                    model_event = _model_actual_event(model, reported_model)
                                    if model_event:
                                        _anth_model_announced = True
                                        yield model_event
                            _u = (
                                message_data.get("usage")
                                if isinstance(message_data, dict)
                                else {}
                            ) or {}
                            if not isinstance(_u, dict):
                                _u = {}
                            if "input_tokens" in _u:
                                _anth_usage_seen = True
                            _anth_input_tokens = _u.get("input_tokens", 0)
                            # Surface prompt-cache effectiveness: cache_read > 0 means the
                            # stable system+tools prefix was served from cache this round.
                            _c_read = _u.get("cache_read_input_tokens", 0)
                            _c_write = _u.get("cache_creation_input_tokens", 0)
                            if _c_read or _c_write:
                                logger.info(
                                    "[anthropic-cache] read=%s write=%s fresh_input=%s",
                                    _c_read, _c_write, _anth_input_tokens,
                                )
                        elif evt == "message_delta":
                            _u = j.get("usage") or {}
                            if not isinstance(_u, dict):
                                _u = {}
                            if "output_tokens" in _u:
                                _anth_usage_seen = True
                            _anth_output_tokens = _u.get("output_tokens", 0)
                            _delta_obj = j.get("delta") or {}
                            if isinstance(_delta_obj, dict) and _delta_obj.get("stop_reason"):
                                _anth_stop_reason = _delta_obj.get("stop_reason")
                        elif evt == "message_stop":
                            # MOD-03: a safety refusal is signalled only via this
                            # stop_reason, on top of whatever text already streamed
                            # as normal `delta` events — surface it as its own typed
                            # event instead of letting the turn look like an
                            # ordinary completion.
                            if _anth_stop_reason == "refusal":
                                yield _stream_refusal_event(stop_reason=_anth_stop_reason)
                            # Emit accumulated tool calls in OpenAI-compatible format
                            if _anth_tool_blocks:
                                calls = []
                                for idx in sorted(_anth_tool_blocks):
                                    tb = _anth_tool_blocks[idx]
                                    calls.append({
                                        "id": tb["id"],
                                        "name": tb["name"],
                                        "arguments": tb["arguments"],
                                    })
                                _delta_emitted = True
                                yield f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'
                            normalized_usage = _normalize_usage_counts(
                                _anth_input_tokens,
                                _anth_output_tokens,
                            )
                            if normalized_usage and _anth_usage_seen:
                                # `_u` still holds the last `message_delta.usage` dict
                                # seen this round — OpenRouter's Anthropic-compatible
                                # messages endpoint (provider == "openrouter", model
                                # startswith "anthropic/") reports cost there the same
                                # shape as its OpenAI-compatible `usage` object.
                                normalized_usage.update(_extract_usage_extras(_u))
                                _annotate_usage_model(
                                    normalized_usage,
                                    model,
                                    _anth_actual_model,
                                )
                                yield f'data: {json.dumps({"type": "usage", "data": normalized_usage})}\n\n'
                            yield "data: [DONE]\n\n"
                            return
                        elif evt == "error":
                            err = j.get("error") or {}
                            err_msg = err.get("message", "Unknown error") if isinstance(err, dict) else str(err)
                            status = _provider_stream_error_status(err, default=400)
                            yield f'event: error\ndata: {json.dumps({"error": err_msg, "status": status})}\n\n'
                            return
                    except json.JSONDecodeError:
                        continue
                yield "data: [DONE]\n\n"
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            _cooled = _mark_host_dead(target_url)
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
            )
            _should_retry = _should_retry and not _cooled
            _tail = f" — host cooled for {DEAD_HOST_COOLDOWN:.0f}s" if _cooled else " — transient, will retry"
            logger.warning(f"Anthropic stream connect to {target_url} failed: {e}{_tail}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    503, f"Cannot reach {_host_key(target_url)}: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.ReadTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
            )
            logger.warning(
                f"Anthropic stream read timed out (attempt {_attempt}): {e} "
                f"— the request may have completed upstream even though no full response arrived"
            )
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504,
                    "Read timeout after partial output; outcome unknown"
                    if _delta_emitted else
                    f"Read timeout after {_attempt} attempt(s); outcome unknown",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=False,  # outcome_unknown: never auto-retryable for an external caller (§34.5)
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.PoolTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Anthropic stream connection pool timed out (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504, f"Connection pool timeout after {_attempt} attempt(s)",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.WriteTimeout as e:
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Anthropic stream upstream timeout (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    504, f"Upstream timeout after {_attempt} attempt(s)",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.ProtocolError as e:
            # RemoteProtocolError (server reset/violated protocol mid-stream)
            # classifies outcome_unknown; a local one classifies retry_backoff
            # — see retry_policy.classify_http.
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Anthropic stream protocol failure (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    502, f"Upstream protocol error: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except httpx.NetworkError as e:
            # ReadError (reset while reading the response) classifies
            # outcome_unknown; WriteError/CloseError classify retry_backoff.
            _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
                delta_emitted=_delta_emitted,
                fail_fast=availability_only_transport,
            )
            logger.warning(f"Anthropic stream network failure (attempt {_attempt}): {e}")
            async for chunk in _stream_retry_or_fail(
                should_retry=_should_retry, wait=_wait,
                error_chunk=_stream_error_chunk(
                    502, f"Network error: {e}",
                    error_class=_retry_error_class(exc=e), attempts=_attempt,
                    retryable=_retryable,
                    partial=_delta_emitted, fallback_eligible=False,
                ),
                retry_call=lambda: _retry(_attempt + 1),
            ):
                yield chunk
        except Exception as e:
            # Not a recognised transport failure (e.g. a bug in the parsing
            # above): never retried, same as before this module.
            logger.error(f"Anthropic stream error: {e}")
            yield _stream_error_chunk(
                502, str(e), error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=False, partial=_delta_emitted, fallback_eligible=False,
            )
        return

    # ── OpenAI-compatible streaming ──
    # Accumulate native tool_calls across streaming chunks. ToolCallAssembler
    # owns the index/id bookkeeping (incl. providers that omit `index` on
    # parallel calls) and the split-safe JSON reassembly; this stream loop
    # only feeds it deltas and renders its output in the legacy wire shape.
    _tc_assembler = ToolCallAssembler()
    _tool_arg_checked: Dict[int, int] = {}  # call index -> args length last checked
    # For thinking models: prepend <think> to first content delta so frontend
    # can detect thinking-in-progress (some models output </think> but no <think>)
    _thinking_model = _supports_thinking(model)
    _first_content_sent = False
    _in_think_tag = False        # True while consuming <think>…</think> content
    _think_open_stripped = False  # opening <think> tag already removed
    _harmony_router = _HarmonyStreamRouter()
    _harmony_active = False       # sticky: gpt-oss harmony <|channel|> stream detected
    _actual_model = ""
    _actual_model_announced = False
    # Provider finish_reason ("stop" | "length" | "tool_calls" | …). Surfaced
    # as a `finish` event right before [DONE] so the agent loop can tell a
    # truncated answer from a finished one instead of treating both as done.
    _finish_reason: Optional[str] = None
    _delta_emitted = False

    def _emit_tool_calls():
        """Build the tool_calls event string if any were accumulated."""
        if not _tc_assembler.has_calls():
            return None
        calls = _tc_assembler.legacy_calls()
        return f'data: {json.dumps({"type": "tool_calls", "calls": calls})}\n\n'

    def _emit_finish():
        """Provider finish_reason as a typed event (the agent loop uses it to
        auto-continue on 'length'). Nothing is emitted when the provider never
        sent one and no tool call was accumulated — an empty stream stays a
        bare [DONE], as before."""
        reason = _finish_reason
        if _tc_assembler.has_calls() and reason in (None, "stop"):
            reason = "tool_calls"
        if reason is None:
            return None
        return f'data: {json.dumps({"type": "finish", "finish_reason": reason})}\n\n'

    def _format_routed_content(parts: List[Tuple[str, bool]]) -> List[str]:
        nonlocal _first_content_sent, _delta_emitted
        events = []
        for part, is_thinking in parts:
            if is_thinking:
                events.append(_stream_delta_event(part, thinking=True))
                continue
            # Some thinking backends start normal content with a stray closing
            # tag. Repair only that shape; do not wrap every first token for
            # model families like MiniMax, which often stream ordinary answers.
            if _thinking_model and not _first_content_sent and part.lstrip().lower().startswith("</think"):
                part = "<think>" + part
            _first_content_sent = True
            events.append(_stream_delta_event(part))
        if events:
            _delta_emitted = True
        return events

    def _retry(next_attempt: int):
        return _stream_llm_inner(
            url, model, messages, temperature=temperature, max_tokens=max_tokens,
            headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
            session_id=session_id, tool_choice_none=tool_choice_none,
            gen_overrides=gen_overrides, max_retries=max_retries,
            availability_only_transport=availability_only_transport,
            _attempt=next_attempt, _budget=_budget, _engine_wait_used=_engine_wait_used,
        )

    def _retry_after_engine_recovery():
        """One extra attempt, past this call's own `max_retries`/budget,
        after `src.engine_swap.recover_after_connect_failure` brought the
        managed engine behind `target_url` back — a fresh attempt count and time
        budget (the wait must not eat into either), and `_engine_wait_used`
        forced True so this can only ever happen once per top-level call."""
        fresh_budget = RetryBudget(LLMConfig.RETRY_TIME_BUDGET)
        fresh_budget.start()
        return _stream_llm_inner(
            url, model, messages, temperature=temperature, max_tokens=max_tokens,
            headers=headers, timeout=timeout, prompt_type=prompt_type, tools=tools,
            session_id=session_id, tool_choice_none=tool_choice_none,
            gen_overrides=gen_overrides, max_retries=max_retries,
            availability_only_transport=availability_only_transport,
            _attempt=1, _budget=fresh_budget, _engine_wait_used=True,
        )

    try:
        client = _get_http_client()
        h = await apply_kimi_code_headers_async(client, h, target_url)
        async with client.stream('POST', target_url, json=payload, headers=h, timeout=stream_timeout) as r:
            _clear_host_dead(target_url)
            if r.status_code != 200:
                raw = (await r.aread()).decode(errors="replace")
                friendly = _format_upstream_error(r.status_code, raw, target_url)
                _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
                    status=r.status_code, headers=r.headers, attempt=_attempt,
                    max_retries=max_retries, budget=_budget, delta_emitted=False,
                )
                if _should_retry and parse_retry_after(r.headers) is not None:
                    logger.info(
                        f"Stream to {target_url}: server busy (HTTP {r.status_code}), "
                        f"retrying in {_wait:.1f}s (Retry-After honoured)"
                    )
                async for chunk in _stream_retry_or_fail(
                    should_retry=_should_retry, wait=_wait,
                    error_chunk=_stream_status_error_chunk(
                        r.status_code, friendly, raw,
                        error_class=_retry_error_class(status=r.status_code),
                        attempts=_attempt,
                        retryable=_retryable,
                    ),
                    retry_call=lambda: _retry(_attempt + 1),
                ):
                    yield chunk
                return

            async for line in r.aiter_lines():
                if not line:
                    continue

                # SSE allows "data:value" with no space after the colon; gating
                # on "data: " silently dropped content + usage from providers
                # that omit it.
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data == "[DONE]":
                        for event in _format_routed_content(_harmony_router.flush()):
                            yield event
                        tc_event = _emit_tool_calls()
                        if tc_event:
                            yield tc_event
                        fin_event = _emit_finish()
                        if fin_event:
                            yield fin_event
                        yield "data: [DONE]\n\n"
                        return

                    try:
                        if data.strip():
                            if data.startswith("{"):
                                j = json.loads(data)
                                if j.get("error"):
                                    err = j.get("error")
                                    status = _provider_stream_error_status(err, default=400)
                                    text = err.get("message") if isinstance(err, dict) else str(err)
                                    yield f'event: error\ndata: {json.dumps({"error": text or "Upstream request failed", "status": status})}\n\n'
                                    return
                                chunk_model = j.get("model")
                                if isinstance(chunk_model, str) and chunk_model.strip():
                                    _actual_model = chunk_model.strip()
                                    if (
                                        not _actual_model_announced
                                        and not _same_model_identity(_actual_model, model)
                                    ):
                                        _actual_model_announced = True
                                        yield f'data: {json.dumps({"type": "model_actual", "requested_model": model, "model": _actual_model})}\n\n'
                                # Usage chunk (from stream_options)
                                _choices = j.get("choices") or []
                                _delta0 = _choices[0].get("delta") if (_choices and _choices[0] is not None) else None
                                # Capture usage whenever the chunk carries it and
                                # the delta has no actual output. Some gateways /
                                # local servers attach usage to the FINAL delta,
                                # which also carries role/finish_reason (so it is
                                # not exactly None/{}/{"content": None}); gating on
                                # those exact shapes discarded their token counts.
                                _delta_has_output = isinstance(_delta0, dict) and (
                                    _delta0.get("content")
                                    or _delta0.get("reasoning_content")
                                    or _delta0.get("reasoning")
                                    or _delta0.get("thinking")
                                    or _delta0.get("tool_calls")
                                )
                                u = j.get("usage")
                                _has_genuine_usage = (
                                    isinstance(u, dict)
                                    and (
                                        "prompt_tokens" in u
                                        or "completion_tokens" in u
                                    )
                                )
                                if _has_genuine_usage and not _delta_has_output:
                                    _usage_data = _normalize_usage_counts(
                                        u.get("prompt_tokens", 0),
                                        u.get("completion_tokens", 0),
                                    )
                                    if _usage_data is None:
                                        continue
                                    # OBJ-8/A1: OpenRouter's real cost (usage.cost etc,
                                    # only present when the payload opted in with
                                    # usage.include — src/openrouter_options.py) lands
                                    # on this exact `usage` object, since OpenRouter
                                    # speaks the OpenAI-compatible chat/completions
                                    # shape. Absent for every other backend.
                                    _usage_data.update(_extract_usage_extras(u))
                                    # llama.cpp puts a `timings` block alongside `usage` with the
                                    # TRUE generation speed (predicted_per_second) — pure decode,
                                    # excluding prefill/network. Pass it through so the UI shows the
                                    # real gen t/s instead of recomputing tokens/wall-clock (which
                                    # includes prefill and reads ~20-40% low). Prefill speed too.
                                    _tm = j.get("timings")
                                    if isinstance(_tm, dict):
                                        if _tm.get("predicted_per_second"):
                                            _usage_data["gen_tps"] = round(_tm["predicted_per_second"], 2)
                                        if _tm.get("prompt_per_second"):
                                            _usage_data["prefill_tps"] = round(_tm["prompt_per_second"], 2)
                                        # INF-03: the same phase breakdown as the
                                        # Ollama branch above, from llama.cpp's own
                                        # `prompt_ms`/`predicted_ms`/`*_n` — these were
                                        # also being thrown away next to the two rates.
                                        # `load_ms` stays null: llama.cpp does not
                                        # report load time per request.
                                        _usage_data["engine_timings"] = _llamacpp_engine_timings(_tm)
                                    if _actual_model:
                                        _usage_data["model"] = _actual_model
                                        if not _same_model_identity(_actual_model, model):
                                            _usage_data["requested_model"] = model
                                    yield f'data: {json.dumps({"type": "usage", "data": _usage_data})}\n\n'
                                elif "choices" in j:
                                    _c0 = (j["choices"] or [None])[0]
                                    if _c0 is None:
                                        continue
                                    _fr = _c0.get("finish_reason")
                                    if isinstance(_fr, str) and _fr:
                                        _finish_reason = _fr
                                    delta = _c0.get("delta") or {}
                                    if isinstance(delta, dict):
                                        # Text content
                                        # Reasoning tokens (VLLM --reasoning-parser, e.g. Qwen3/DeepSeek-R1, Nemotron). vLLM 0.20.2 / NIM emit the field as `reasoning`; older builds use `reasoning_content`. Some OpenAI-compatible Ollama builds use `thinking`.
                                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or delta.get("thinking") or ""
                                        content = delta.get("content") or ""
                                        # MOD-03: a structured refusal (OpenAI-compatible `delta.refusal`)
                                        # streams on its own field, separate from `content` — surface it
                                        # as its own typed event instead of leaving it unhandled/dropped.
                                        refusal = delta.get("refusal") or ""
                                        # Mistral structured content: content is a list of typed blocks
                                        # ({"type": "thinking", ...}, {"type": "text", ...}). Split into
                                        # reasoning + text so thinking streams into the thinking panel.
                                        if isinstance(content, list):
                                            text_part, thinking_part = _normalize_mistral_content(content)
                                            if thinking_part:
                                                reasoning = (reasoning + thinking_part) if reasoning else thinking_part
                                            content = text_part
                                        if reasoning:
                                            try:
                                                degenerate_guard.check_reasoning(reasoning)
                                            except DegenerateOutput as _degenerate:
                                                yield _degenerate_output_error_chunk(_degenerate)
                                                return
                                            _delta_emitted = True
                                            yield _stream_delta_event(reasoning, thinking=True)
                                        if refusal:
                                            _delta_emitted = True
                                            yield _stream_refusal_event(refusal)
                                        if content:
                                            content = _strip_visible_chat_template_artifacts(content)
                                            if not content:
                                                continue
                                            try:
                                                degenerate_guard.check(content)
                                            except DegenerateOutput as _degenerate:
                                                yield _degenerate_output_error_chunk(_degenerate)
                                                return
                                            content = re.sub(r"<mm:think(\s+[^>]*)?>", r"<think\1>", content, flags=re.IGNORECASE)
                                            content = re.sub(r"</mm:think>", "</think>", content, flags=re.IGNORECASE)
                                            stripped = content.lstrip()
                                            # gpt-oss harmony format (<|channel|>analysis/final): route via the harmony
                                            # stream router. Sticky once the first marker appears — distinct from the
                                            # <think> path below (handled in the else, preserving #2588 behaviour).
                                            if _harmony_active or "<|" in content:
                                                _harmony_active = True
                                                for event in _format_routed_content(_harmony_router.feed(content)):
                                                    yield event
                                            else:
                                                # Auto-detect <think>…</think> in content stream.
                                                # Covers Qwen3-derived models (Qwopus, QwQ forks) whose
                                                # names don't match _THINKING_MODEL_PATTERNS but still
                                                # emit literal <think> markup via llama.cpp --jinja.
                                                if not _first_content_sent and not _thinking_model and not _in_think_tag and stripped.lower().startswith("<think"):
                                                    _thinking_model = True
                                                    _in_think_tag = True
                                                if _in_think_tag:
                                                    close_idx = content.lower().find("</think>")
                                                    if close_idx != -1:
                                                        # Split: up-to-</think> → thinking, remainder → content
                                                        think_part = content[:close_idx]
                                                        if not _think_open_stripped:
                                                            # Strip the opening <think[...] > from the first chunk.
                                                            # Use a dedicated flag — _first_content_sent stays False
                                                            # throughout the think block, so it must not be reused.
                                                            tag_end = think_part.lower().find(">")
                                                            if tag_end != -1:
                                                                think_part = think_part[tag_end + 1:]
                                                            _think_open_stripped = True
                                                        regular_part = content[close_idx + len("</think>"):]
                                                        _in_think_tag = False
                                                        if think_part:
                                                            _delta_emitted = True
                                                            yield f'data: {json.dumps({"delta": think_part, "thinking": True})}\n\n'
                                                        if regular_part:
                                                            _first_content_sent = True
                                                            _delta_emitted = True
                                                            yield f'data: {json.dumps({"delta": regular_part})}\n\n'
                                                    else:
                                                        # Still inside <think>: route to thinking channel
                                                        if not _think_open_stripped:
                                                            # Strip the opening <think[...] > tag (first chunk only)
                                                            tag_end = stripped.lower().find(">")
                                                            if tag_end != -1:
                                                                content = stripped[tag_end + 1:]
                                                            _think_open_stripped = True
                                                        if content:
                                                            _delta_emitted = True
                                                            yield f'data: {json.dumps({"delta": content, "thinking": True})}\n\n'
                                                else:
                                                    # Some thinking backends start normal content with a
                                                    # stray closing tag. Repair only that shape; do not
                                                    # wrap every first token for model families like
                                                    # MiniMax, which often stream ordinary answers.
                                                    if _thinking_model and not _first_content_sent and stripped.lower().startswith("</think"):
                                                        content = "<think>" + content
                                                    _first_content_sent = True
                                                    _delta_emitted = True
                                                    yield f'data: {json.dumps({"delta": content})}\n\n'
                                        # Native tool calls — accumulate across chunks via the
                                        # assembler (index/id bookkeeping + split-safe JSON
                                        # reassembly). Only the harmony name-unaliasing stays
                                        # here, since it is model-family-specific and the
                                        # assembler itself must not know about it.
                                        for tc in delta.get("tool_calls") or []:
                                            if tc is None:
                                                continue
                                            func = tc.get("function") or {}
                                            if func.get("name"):
                                                # Map harmony aliases back to real tool names
                                                # before anything downstream (including the
                                                # assembler's own record) sees them.
                                                tc = dict(tc)
                                                tc["function"] = dict(func, name=_unalias_harmony_tool_name(func["name"], model))
                                                func = tc["function"]
                                            call = _tc_assembler.feed(tc)
                                            if call and call.last_appended:
                                                # Feed the raw argument delta through the same
                                                # guard as content/reasoning: a broken model
                                                # server can emit gibberish ("////", token soup)
                                                # as tool-call argument text too, and the
                                                # server-side tool-call parser holds that text
                                                # until the call is complete, so it would
                                                # otherwise never reach a guard until the
                                                # loop-specific check below (which needs 1500+
                                                # chars to build up) — see §179.
                                                try:
                                                    degenerate_guard.check(call.last_appended)
                                                except DegenerateOutput as _degenerate:
                                                    yield _degenerate_output_error_chunk(_degenerate)
                                                    return
                                                _args_len = len(call.arguments)
                                                _last = _tool_arg_checked.get(call.index, 0)
                                                if (_args_len >= _TOOL_ARG_LOOP_START_CHARS
                                                        and _args_len - _last >= _TOOL_ARG_LOOP_EVERY_CHARS):
                                                    _tool_arg_checked[call.index] = _args_len
                                                    _loop = (tool_argument_loop(call.arguments)
                                                             or tool_argument_template_loop(call.arguments))
                                                    if _loop:
                                                        yield _degenerate_output_error_chunk(
                                                            DegenerateOutput(f"{call.name or 'tool call'}: {_loop}", model))
                                                        return
                                            # Stream tool arg deltas for doc tools. Guards against a
                                            # null arguments delta the same way the assembler does
                                            # internally: `func` can be {"arguments": None} (JSON
                                            # null), which must not raise or get treated as a delta.
                                            if call and call.last_appended and call.name in ("create_document", "update_document", "edit_document"):
                                                _delta_emitted = True
                                                yield f'data: {json.dumps({"type": "tool_call_delta", "index": call.index, "name": call.name, "arg_delta": call.last_appended})}\n\n'
                                elif "text" in j:
                                    if j["text"]:
                                        for event in _format_routed_content(_harmony_router.feed(j["text"])):
                                            yield event
                            else:
                                if data.strip():
                                    for event in _format_routed_content(_harmony_router.feed(data)):
                                        yield event
                    except Exception as e:
                        logger.error(f"Error parsing stream data: {e}")
                        continue

            # End of stream (no explicit [DONE] received)
            for event in _format_routed_content(_harmony_router.flush()):
                yield event
            tc_event = _emit_tool_calls()
            if tc_event:
                yield tc_event
            fin_event = _emit_finish()
            if fin_event:
                yield fin_event
            yield "data: [DONE]\n\n"

    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        _cooled = _mark_host_dead(target_url)
        _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
            exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
            delta_emitted=_delta_emitted,
        )
        _should_retry = _should_retry and not _cooled
        if not _should_retry and not _delta_emitted and not _engine_wait_used:
            from src import engine_swap
            if engine_swap.restartable_engine_for_url(target_url) is not None:
                logger.warning(
                    "Stream connect to %s: retries exhausted on a managed engine — "
                    "starting it again before failing the turn",
                    _host_key(target_url),
                )
                if await engine_swap.recover_after_connect_failure(target_url):
                    _clear_host_dead(target_url)
                    async for chunk in _retry_after_engine_recovery():
                        yield chunk
                    return
        _tail = f" — host cooled for {DEAD_HOST_COOLDOWN:.0f}s" if _cooled else " — transient, will retry"
        logger.warning(f"Stream connect to {target_url} failed: {e}{_tail}")
        async for chunk in _stream_retry_or_fail(
            should_retry=_should_retry, wait=_wait,
            error_chunk=_stream_error_chunk(
                503, f"Cannot reach {_host_key(target_url)}: {e}",
                error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=_retryable,
                partial=_delta_emitted,
            ),
            retry_call=lambda: _retry(_attempt + 1),
        ):
            yield chunk
    except httpx.ReadTimeout as e:
        _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
            exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
            delta_emitted=_delta_emitted,
        )
        logger.warning(
            f"Stream read timed out (attempt {_attempt}): {e} "
            f"— the request may have completed upstream even though no full response arrived"
        )
        async for chunk in _stream_retry_or_fail(
            should_retry=_should_retry, wait=_wait,
            error_chunk=_stream_error_chunk(
                504,
                "Read timeout after partial output; outcome unknown"
                if _delta_emitted else
                f"Read timeout after {_attempt} attempt(s); outcome unknown",
                error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=False,  # outcome_unknown: never auto-retryable for an external caller (§34.5)
                partial=_delta_emitted,
            ),
            retry_call=lambda: _retry(_attempt + 1),
        ):
            yield chunk
    except httpx.PoolTimeout as e:
        _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
            exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
            delta_emitted=_delta_emitted,
            fail_fast=availability_only_transport,
        )
        logger.warning(f"Stream connection pool timed out (attempt {_attempt}): {e}")
        async for chunk in _stream_retry_or_fail(
            should_retry=_should_retry, wait=_wait,
            error_chunk=_stream_error_chunk(
                504, f"Connection pool timeout after {_attempt} attempt(s)",
                error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=_retryable,
                partial=_delta_emitted,
            ),
            retry_call=lambda: _retry(_attempt + 1),
        ):
            yield chunk
    except httpx.WriteTimeout as e:
        _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
            exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
            delta_emitted=_delta_emitted,
            fail_fast=availability_only_transport,
        )
        logger.warning(f"Stream upstream timeout (attempt {_attempt}): {e}")
        async for chunk in _stream_retry_or_fail(
            should_retry=_should_retry, wait=_wait,
            error_chunk=_stream_error_chunk(
                504, f"Upstream timeout after {_attempt} attempt(s)",
                error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=_retryable,
                partial=_delta_emitted, fallback_eligible=False,
            ),
            retry_call=lambda: _retry(_attempt + 1),
        ):
            yield chunk
    except httpx.ProtocolError as e:
        # RemoteProtocolError (server reset/violated protocol mid-stream)
        # classifies outcome_unknown; a local one classifies retry_backoff
        # — see retry_policy.classify_http.
        _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
            exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
            delta_emitted=_delta_emitted,
            fail_fast=availability_only_transport,
        )
        logger.warning(f"Stream protocol failure (attempt {_attempt}): {e}")
        async for chunk in _stream_retry_or_fail(
            should_retry=_should_retry, wait=_wait,
            error_chunk=_stream_error_chunk(
                502, f"Upstream protocol error: {e}",
                error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=_retryable,
                partial=_delta_emitted, fallback_eligible=False,
            ),
            retry_call=lambda: _retry(_attempt + 1),
        ):
            yield chunk
    except httpx.NetworkError as e:
        # ReadError (reset while reading the response) classifies
        # outcome_unknown; WriteError/CloseError classify retry_backoff.
        _should_retry, _wait, _classification, _retryable = _stream_retry_decision(
            exc=e, attempt=_attempt, max_retries=max_retries, budget=_budget,
            delta_emitted=_delta_emitted,
            fail_fast=availability_only_transport,
        )
        logger.warning(f"Stream network failure (attempt {_attempt}): {e}")
        async for chunk in _stream_retry_or_fail(
            should_retry=_should_retry, wait=_wait,
            error_chunk=_stream_error_chunk(
                502, f"Network error: {e}",
                error_class=_retry_error_class(exc=e), attempts=_attempt,
                retryable=_retryable,
                partial=_delta_emitted, fallback_eligible=False,
            ),
            retry_call=lambda: _retry(_attempt + 1),
        ):
            yield chunk
    except Exception as e:
        # Not a recognised transport failure (e.g. a bug in the parsing
        # above): never retried, same as before this module.
        logger.error(f"Stream error: {e}")
        yield _stream_error_chunk(
            502, str(e), error_class=_retry_error_class(exc=e), attempts=_attempt,
            retryable=False, partial=_delta_emitted, fallback_eligible=False,
        )


def _summarize_stream_error(err_chunk: Optional[str]) -> str:
    """Pull a short human reason out of an `event: error` SSE chunk for the
    fallback notice. Returns a generic message if it can't be parsed."""
    if not err_chunk:
        return "primary model failed"
    try:
        for line in err_chunk.split("\n"):
            if line.startswith("data: "):
                j = json.loads(line[6:])
                txt = j.get("text") or j.get("error") or ""
                status = j.get("status")
                msg = (f"HTTP {status}: " if status else "") + str(txt)
                return msg[:200].strip() or "primary model failed"
    except Exception:
        pass
    return "primary model failed"


def _stream_error_status(err_chunk: Optional[str]) -> Optional[int]:
    """Return the integer status from an SSE error chunk when present."""

    if not err_chunk:
        return None
    try:
        for line in err_chunk.split("\n"):
            if not line.startswith("data: "):
                continue
            status = json.loads(line[6:]).get("status")
            return _normalize_http_status(status)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _stream_error_fallback_override(err_chunk: Optional[str]) -> Optional[bool]:
    """Return an adapter's explicit eligibility decision when present."""

    if not err_chunk:
        return None
    try:
        for line in err_chunk.split("\n"):
            if not line.startswith("data: "):
                continue
            value = json.loads(line[6:]).get("fallback_eligible")
            return value if isinstance(value, bool) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _request_factory_error_chunk(error: Exception, status: Optional[int]) -> str:
    """Convert route-request preparation failures into a safe SSE error."""

    wire_status = status if status is not None else 500
    payload = {
        "error": f"Model request preparation failed (HTTP {wire_status})",
        "status": wire_status,
    }
    override = getattr(error, "fallback_eligible", None)
    if isinstance(override, bool):
        payload["fallback_eligible"] = override
    elif status is None:
        # An unclassified internal/configuration failure must never become an
        # availability fallback merely because its safe wire status is 500.
        payload["fallback_eligible"] = False
    return f'event: error\ndata: {json.dumps(payload)}\n\n'


# Symbolic-only rate-limit statuses providers emit without a numeric code.
# RESOURCE_EXHAUSTED is the gRPC/Google symbol for 429; the other two appear
# in OpenAI-compatible proxies. Any other symbolic status still fails closed.
_SYMBOLIC_RATE_LIMIT_STATUSES = frozenset({
    "RATE_LIMITED",
    "RATE_LIMIT_EXCEEDED",
    "RESOURCE_EXHAUSTED",
})


def _provider_stream_error_status(error, *, default: int = 400) -> int:
    """Classify structured provider stream errors without making them eligible by default.

    Some streaming APIs report an HTTP 200 handshake and put the real failure
    in a later event. Unknown application errors are request failures, not
    availability evidence; only explicit transient/server markers become 5xx
    or rate-limit statuses.
    """

    if isinstance(error, dict):
        # A structured numeric status is authoritative. Text heuristics are
        # only a fallback for providers that omit it.
        saw_explicit_status = False
        symbolic_rate_limited = False
        for key in ("status", "status_code", "http_status", "code"):
            if key not in error:
                continue
            value = error.get(key)
            if value is None:
                continue
            # Google-style errors use a symbolic ``status`` together with a
            # numeric HTTP ``code``.  A symbolic ``code`` remains part of the
            # marker heuristics below; it is not itself an explicit status.
            if key != "code":
                saw_explicit_status = True
            if (
                key == "status"
                and isinstance(value, str)
                and value.strip().upper() in _SYMBOLIC_RATE_LIMIT_STATUSES
            ):
                # Only availability evidence when no numeric status follows:
                # a payload pairing a symbolic status with e.g. code=401 must
                # surface the numeric truth, not advance fallback.
                symbolic_rate_limited = True
                continue
            status = _normalize_http_status(value)
            if status is not None:
                return status
        if symbolic_rate_limited:
            return 429
        if saw_explicit_status:
            return default
        marker = " ".join(str(error.get(key) or "") for key in ("type", "code", "message")).lower()
    else:
        marker = str(error or "").lower()

    if "insufficient_quota" in marker or "billing" in marker:
        return 402
    if any(token in marker for token in ("authentication", "unauthorized", "invalid api key", "invalid_api_key")):
        return 401
    if any(token in marker for token in ("permission", "forbidden")):
        return 403
    if any(token in marker for token in ("not_found", "not found", "unknown model")):
        return 404
    if any(token in marker for token in ("invalid_request", "invalid request", "unsupported", "malformed", "bad request")):
        return 400
    if any(token in marker for token in ("rate_limit", "rate limit", "too many requests")):
        return 429
    if any(token in marker for token in ("overloaded", "over capacity")):
        return 529
    if any(token in marker for token in ("timeout", "timed out")):
        return 504
    if any(token in marker for token in ("api_error", "server_error", "server error", "internal error", "temporarily unavailable")):
        return 500

    return default


async def stream_llm_with_fallback(candidates, messages, **kwargs):
    """Wrap stream_llm with an ordered fallback chain.

    `candidates` is a list of (url, model, headers). Each is tried in order,
    but only retried on an eligible *pre-content* failure. Callers can restrict
    errors with ``fallback_statuses`` and disable empty-completion switching
    with ``fallback_on_empty=False``. Omitting both preserves the generic
    fallback behavior for non-foreground call sites.
    Metadata is held until substantive output commits the candidate.
    Once a candidate has emitted real output we never switch (that would
    duplicate streamed tokens); a later error from that candidate passes
    through unchanged. The dead-host cooldown in stream_llm makes repeat
    attempts at an offline primary effectively instant.

    Yields the same SSE chunk protocol as stream_llm.
    """
    fallback_statuses = kwargs.pop("fallback_statuses", None)
    fallback_on_empty = bool(kwargs.pop("fallback_on_empty", True))
    candidate_request_factory = kwargs.pop("candidate_request_factory", None)
    candidate_route_descriptors = kwargs.pop("candidate_route_descriptors", None)
    abort_when = kwargs.pop("abort_when", None)
    eligible_statuses = None if fallback_statuses is None else frozenset(fallback_statuses)

    raw_candidates = list(candidates or [])
    if not raw_candidates or not _candidate_is_configured(raw_candidates[0]):
        yield f'event: error\ndata: {json.dumps({"error": "Selected model endpoint is not configured", "status": 400, "fallback_eligible": False})}\n\n'
        return
    cands, route_descriptors = _dedupe_model_candidates_with_descriptors(
        raw_candidates,
        candidate_route_descriptors,
    )
    # A selected official client is an explicit account/billing choice, even
    # for background callers using the legacy unrestricted fallback policy.
    if str(cands[0][0] or '').startswith('faustus-cli://'):
        cands = cands[:1]
        route_descriptors = route_descriptors[:1]

    primary_model = cands[0][1]
    primary_route = route_descriptors[0]
    last_error = None
    failures = []
    for i, (url, model, headers) in enumerate(cands):
        is_last = (i == len(cands) - 1)
        emitted = False
        retried = False
        pending_metadata = []
        candidate_messages = messages
        candidate_kwargs = kwargs
        if candidate_request_factory is not None:
            try:
                request = candidate_request_factory(i, url, model, headers) or {}
                if hasattr(request, "__await__"):
                    request = await request
                candidate_messages = request.get("messages", messages)
                candidate_kwargs = {**kwargs, **(request.get("kwargs") or {})}
            except Exception as error:
                status = _nonstream_error_status(error)
                eligibility_override = getattr(error, "fallback_eligible", None)
                eligible = (
                    True
                    if eligible_statuses is None
                    else (
                        eligibility_override
                        if isinstance(eligibility_override, bool)
                        else status in eligible_statuses
                    )
                )
                error_chunk = _request_factory_error_chunk(error, status)
                if not is_last and eligible:
                    last_error = error_chunk
                    failures.append({
                        "candidate_index": i,
                        "model": model,
                        "status": status,
                        "reason": _summarize_stream_error(error_chunk),
                    })
                    tag = "primary" if i == 0 else "candidate"
                    logger.warning(
                        "[fallback] %s %s request preparation failed with "
                        "eligible status %s; trying next",
                        tag,
                        model,
                        status,
                    )
                    continue
                yield error_chunk
                return
        candidate_messages = await _vision_filter_for_route(url, model, candidate_messages)
        candidate_stream = stream_llm(
            url,
            model,
            candidate_messages,
            headers=headers,
            **{
                **candidate_kwargs,
                "availability_only_transport": True,
            },
        )
        try:
            async for chunk in candidate_stream:
                if chunk.startswith("event: error"):
                    status = _stream_error_status(chunk)
                    eligibility_override = _stream_error_fallback_override(chunk)
                    eligible = (
                        True
                        if eligible_statuses is None
                        else (
                            eligibility_override
                            if eligibility_override is not None
                            else status in eligible_statuses
                        )
                    )
                    if not emitted and not is_last and eligible:
                        # Pre-content failure with fallbacks left — swallow and
                        # move to the next candidate.
                        last_error = chunk
                        failures.append({
                            "candidate_index": i,
                            "model": model,
                            "status": status,
                            "reason": _summarize_stream_error(chunk),
                        })
                        retried = True
                        if i == 0:
                            logger.warning(f"[fallback] primary {model} failed before output; trying fallback")
                        else:
                            logger.warning(f"[fallback] candidate {model} failed; trying next")
                        break
                    if not emitted:
                        # A last-candidate error is already the clearest terminal
                        # result; do not append an empty-completion error as well.
                        yield chunk
                        return
                    yield chunk
                    continue

                event_data = {}
                is_done = chunk.startswith("data: [DONE]")
                if chunk.startswith("data: ") and not is_done:
                    try:
                        event_data = json.loads(chunk[6:])
                    except Exception:
                        pass

                delta = event_data.get("delta")
                event_type = event_data.get("type")
                substantive = (
                    isinstance(delta, str) and bool(delta.strip())
                ) or (
                    event_type == "tool_calls"
                    and bool(event_data.get("calls"))
                )

                if substantive and not emitted:
                    # First real output from a NON-primary candidate: tell the client
                    # the selected model failed and another answered. Without this the
                    # fallback is invisible — a misconfigured provider looks like it
                    # works because the reply is shown under the originally selected
                    # model's name (e.g. a Bedrock/Claude endpoint that 400s every
                    # request but appears fine because another model silently answered).
                    if i > 0:
                        primary_reason = (
                            failures[0]["reason"]
                            if failures
                            else _summarize_stream_error(last_error)
                        )
                        yield ('data: ' + json.dumps({
                            "type": "fallback",
                            "selected_model": primary_model,
                            "answered_by": model,
                            "selected_endpoint_id": primary_route.get("endpoint_id"),
                            "selected_endpoint_label": primary_route.get("endpoint_label"),
                            "selected_endpoint_cost_tracked": primary_route.get("endpoint_cost_tracked"),
                            "answered_by_endpoint_id": route_descriptors[i].get("endpoint_id"),
                            "answered_by_endpoint_label": route_descriptors[i].get("endpoint_label"),
                            "answered_by_endpoint_cost_tracked": route_descriptors[i].get("endpoint_cost_tracked"),
                            "candidate_index": i,
                            "reason": primary_reason,
                            "failures": [
                                {
                                    "candidate_index": failure["candidate_index"],
                                    "model": failure["model"],
                                    "status": failure["status"],
                                }
                                for failure in failures
                            ],
                        }) + '\n\n')
                    # Metadata must not commit a candidate. Once real output arrives,
                    # flush it after any fallback notice and before the output itself.
                    for metadata_chunk in pending_metadata:
                        yield metadata_chunk
                    pending_metadata.clear()
                    emitted = True

                if substantive or emitted:
                    yield chunk
                    if callable(abort_when):
                        try:
                            if abort_when():
                                break
                        except Exception:
                            pass
                elif not is_done:
                    pending_metadata.append(chunk)
        finally:
            close_candidate = getattr(candidate_stream, "aclose", None)
            if callable(close_candidate):
                try:
                    await close_candidate()
                except Exception as close_error:
                    logger.warning(
                        "[fallback] failed to close candidate %s stream: %s",
                        model,
                        type(close_error).__name__,
                    )

        if emitted:
            return
        if retried:
            continue
        if not is_last and fallback_on_empty:
            last_error = _empty_completion_error_chunk(
                f"Model {model} returned no substantive output"
            )
            failures.append({
                "candidate_index": i,
                "model": model,
                "status": 502,
                "reason": _summarize_stream_error(last_error),
            })
            tag = "primary" if i == 0 else "candidate"
            logger.warning(f"[fallback] {tag} {model} returned no substantive output; trying next")
            continue
        if not is_last:
            yield _empty_completion_error_chunk(
                f"Model {model} returned no substantive output"
            )
            return
        yield _empty_completion_error_chunk(
            "All model candidates returned no substantive output"
        )
        return
