"""src/engine_swap.py — "engine swap" (inspired by llama-swap): managed
llama.cpp engines (src.engines) start on demand when a local model call
targets them, and unload when idle, so the owner's GPU is not held by an
idle `llama-server`.

Two halves:

  * `ensure_ready(url)` — called from `src.llm_core._local_model_slot`
    right before a local model call. Maps the request URL to a managed
    engine (loopback host:port match), and if it is not already healthy,
    starts it and waits for the health probe. Concurrent callers for the
    same engine share one start (single-flight) instead of racing
    `start_engine` — `engines.start_engine` itself refuses a port already
    in use, so a second literal call would just fail loudly for no reason.
    Never raises into the chat path: any problem here is logged and the
    normal request error (connection refused, timeout) surfaces on its own.

  * the idle reaper (`start()`/`stop()`/`status()`, same shape as
    `src.model_warmup`) — every `engine_idle_check_s` seconds, stops any
    managed engine that is healthy, has zero in-flight requests, and has
    been unused for `engine_idle_ttl_minutes` (0 = disabled, the default:
    an owner who wants engines to just stay up leaves this off). An engine
    the reaper has never seen touched (started outside Faustus, or before
    this process came up) counts its idle clock from the first reaper
    tick that saw it healthy — so it is still reaped eventually, not kept
    alive forever just because Faustus itself never called it.

Settings (src/settings.py): `engine_autostart` (True), `engine_autostart_
timeout_s` (180), `engine_idle_ttl_minutes` (0), `engine_idle_check_s` (30).
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

# Per-engine usage bookkeeping: last-use time (monotonic, for TTL math; wall
# clock only for display) and an in-flight counter. Keyed by engine id.
_last_used_mono: Dict[str, float] = {}
_last_used_wall: Dict[str, float] = {}
_in_flight: Dict[str, int] = {}
# When the reaper first saw an engine healthy/running, for engines that were
# never `touch()`-ed by this process (started outside Faustus) — its idle
# clock starts here instead of never ticking at all.
_first_seen_running: Dict[str, float] = {}

# Single-flight starts: one in-progress `ensure_ready` per engine id: extra
# callers await the same task instead of each calling `start_engine`.
_starts: Dict[str, "asyncio.Task"] = {}

_task: Optional[asyncio.Task] = None


def _settings() -> Dict[str, Any]:
    try:
        from src.settings import get_setting
        return {
            "autostart": bool(get_setting("engine_autostart", True)),
            "autostart_timeout_s": float(get_setting("engine_autostart_timeout_s", 180) or 180),
            "idle_ttl_minutes": float(get_setting("engine_idle_ttl_minutes", 0) or 0),
            "idle_check_s": float(get_setting("engine_idle_check_s", 30) or 30),
        }
    except Exception:  # noqa: BLE001
        return {"autostart": True, "autostart_timeout_s": 180.0, "idle_ttl_minutes": 0.0, "idle_check_s": 30.0}


def _canon_host(host: str) -> str:
    h = (host or "").strip().lower()
    return "127.0.0.1" if h in _LOOPBACK_HOSTS else h


def engine_for_url(url: str) -> Optional[Dict[str, Any]]:
    """The managed engine (src.engines) whose loopback host:port matches
    `url`, else None. Cheap: no network, just a scan of configured engines."""
    try:
        parsed = urlparse(url or "")
    except Exception:  # noqa: BLE001
        return None
    host = _canon_host(parsed.hostname or "")
    port = parsed.port
    if not host or port is None or host not in {"127.0.0.1"}:
        return None
    try:
        from src import engines
    except Exception:  # noqa: BLE001
        return None
    try:
        for engine in engines.list_engines():
            if _canon_host(str(engine.get("host") or "")) == host and engine.get("port") == port:
                return engine
    except Exception as exc:  # noqa: BLE001
        logger.debug("[engine-swap] engine_for_url lookup failed: %s", exc)
        return None
    return None


def touch(url_or_engine_id: str) -> None:
    """Record 'this engine was used right now'. Accepts either a request URL
    (resolved via `engine_for_url`) or an engine id directly."""
    engine_id = _resolve_engine_id(url_or_engine_id)
    if engine_id is None:
        return
    _last_used_mono[engine_id] = time.monotonic()
    _last_used_wall[engine_id] = time.time()


def _resolve_engine_id(url_or_engine_id: str) -> Optional[str]:
    s = str(url_or_engine_id or "")
    if "://" in s:
        engine = engine_for_url(s)
        return engine.get("id") if engine else None
    return s or None


def begin(url_or_engine_id: str) -> Optional[str]:
    """Mark one in-flight request against the engine `url_or_engine_id` maps
    to; returns the engine id (for `end`), or None if it maps to nothing."""
    engine_id = _resolve_engine_id(url_or_engine_id)
    if engine_id is None:
        return None
    _in_flight[engine_id] = _in_flight.get(engine_id, 0) + 1
    return engine_id


def end(engine_id: Optional[str]) -> None:
    if engine_id is None:
        return
    _in_flight[engine_id] = max(0, _in_flight.get(engine_id, 0) - 1)


@asynccontextmanager
async def in_use(url_or_engine_id: str):
    """Context manager: bump in-flight for the mapped engine (if any) for the
    duration, and `touch()` it on the way out."""
    engine_id = begin(url_or_engine_id)
    try:
        yield engine_id
    finally:
        end(engine_id)
        if engine_id is not None:
            touch(engine_id)


async def _probe(engine: Dict[str, Any]) -> str:
    """Cheap `/health` check: "ok", "loading" (llama-server answers 503 while
    it loads weights) or "down" (nothing answers). One small GET, because
    this runs before every local model call."""
    try:
        import httpx
        root = f"http://{engine.get('host') or '127.0.0.1'}:{engine.get('port')}"
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(root + "/health")
        if resp.status_code == 200:
            return "ok"
        return "loading" if resp.status_code == 503 else "down"
    except Exception as exc:  # noqa: BLE001
        logger.debug("[engine-swap] health probe failed: %s", exc)
        return "down"


async def _probe_healthy(engine: Dict[str, Any]) -> bool:
    return (await _probe(engine)) == "ok"


async def probe_healthy(engine: Dict[str, Any]) -> bool:
    """Public alias of `_probe_healthy` for callers outside this module
    (`src.model_warmup`'s llama.cpp residency branch) — one probe helper,
    not a second copy of it."""
    return await _probe_healthy(engine)


async def _wait_healthy(engine: Dict[str, Any], timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if await _probe_healthy(engine):
            return True
        await asyncio.sleep(1.0)
    return False


async def _do_start_and_wait(engine_id: str, timeout_s: float) -> Dict[str, Any]:
    from src import engines
    engine = engines.get_engine(engine_id)
    if engine is None:
        return {"engine_id": engine_id, "action": "failed", "error": "engine not found"}
    try:
        result = await engines.start_engine(engine_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[engine-swap] start_engine(%s) raised: %s", engine_id, exc)
        return {"engine_id": engine_id, "action": "failed", "error": str(exc)}
    if not result.get("started"):
        err = result.get("error") or "start failed"
        logger.warning("[engine-swap] failed to start engine %s: %s", engine_id, err)
        return {"engine_id": engine_id, "action": "failed", "error": err}

    logger.info("[engine-swap] starting engine %s on demand", engine_id)
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if await _probe_healthy(engine):
            logger.info("[engine-swap] engine %s is ready", engine_id)
            touch(engine_id)
            return {"engine_id": engine_id, "action": "started"}
        await asyncio.sleep(1.0)
    logger.warning("[engine-swap] engine %s did not become healthy within %.0fs", engine_id, timeout_s)
    return {"engine_id": engine_id, "action": "failed", "error": "timed out waiting for engine to become healthy"}


def restartable_engine_for_url(url: str) -> Optional[Dict[str, Any]]:
    """The managed engine behind `url` when on-demand start is on, else None."""
    if not _settings()["autostart"]:
        return None
    return engine_for_url(url)


async def recover_after_connect_failure(url: str) -> bool:
    """A local model call lost its engine mid-turn: bring it back.

    `ensure_ready` runs once, before a call. Live (24-09): the engine died
    in the middle of a two-hour agent turn, every retry of that call hit a
    closed port, and the turn failed with HTTP 502 although the engine was
    a managed one this module would have started on the next call. The
    caller (`src.llm_core`, once its own retries are spent, only if nothing
    was streamed yet) calls this; it starts the engine again the same way
    `ensure_ready` does and says whether it is healthy, so the call can be
    tried once more. Never raises."""
    try:
        engine = restartable_engine_for_url(url)
        if engine is None:
            return False
        logger.warning("[engine-swap] lost engine %s mid-call; starting it again", engine.get("id"))
        result = await ensure_ready(url)
        if result.get("action") in ("started", "waited"):
            return True
        if result.get("action") == "none":
            return await _probe_healthy(engine)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("[engine-swap] recovery failed for %s: %s", url, exc)
        return False


_GARBAGE_RE = None
_RESTART_PAUSE_S = 2.0  # let the port close before starting it again
_SANITY_PROMPT = "Reply with one short friendly greeting."


def _is_garbage(text: str) -> bool:
    """A reply made of one short unit repeated: symbols (``////``, ``0000``)
    or letters (``жнымжнымжным…``, seen live on 26-09 after an exam: the
    27B answered "The capital of France is" with one Cyrillic syllable
    twelve times, which the letters-free rule let through). The sanity
    prompt asks for a greeting, so a unit of up to 8 characters filling
    the reply is never a real answer."""
    import re
    global _GARBAGE_RE
    if _GARBAGE_RE is None:
        _GARBAGE_RE = re.compile(r"^(.{1,8}?)\1{4,}.{0,8}$", re.DOTALL)
    body = "".join(str(text or "").split())
    m = _GARBAGE_RE.match(body) if body else None
    if not m:
        return False
    unit = m.group(1)
    if not any(ch.isalpha() for ch in unit):
        return True
    # Letters: a laugh ("hahaha") is a model's choice, not a broken engine;
    # a syllable of 3+ letters or one outside ASCII filling the whole reply
    # to a greeting prompt is.
    return len(unit) >= 3 or any(ord(ch) > 127 for ch in unit)


async def generates_sanely(url: str, model: str, *, timeout_s: float = 30.0) -> Optional[bool]:
    """Ask the engine behind `url` for a one-line greeting with reasoning off.

    False when it answers with one symbol repeated -- the state seen live
    (25-09) in which a llama-server on a GPU that another process had just
    squeezed answered every prompt, even "Di hola.", with ``/`` until it was
    restarted. None when the probe itself could not run (no answer, not a
    chat endpoint): nothing is concluded from that. Never raises."""
    import httpx
    base = str(url or "").rstrip("/")
    if not base.endswith("/v1"):
        base = base.split("/v1/")[0].rstrip("/") + "/v1"
    body = {"model": model, "messages": [{"role": "user", "content": _SANITY_PROMPT}],
            "max_tokens": 16, "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as client:
            resp = await client.post(base + "/chat/completions", json=body)
        if resp.status_code != 200:
            return None
        msg = ((resp.json().get("choices") or [{}])[0].get("message") or {})
        text = str(msg.get("content") or "") + str(msg.get("reasoning_content") or "")
        if not text.strip():
            return None
        return not _is_garbage(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[engine-swap] sanity probe failed for %s: %s", url, exc)
        return None


async def restart_if_garbled(url: str, model: str) -> bool:
    """Restart the managed engine behind `url` when it only produces garbage.

    Called by the agent harness after a token-repeat collapse. A collapse is
    usually the model's own doing (a loop, a bad sampler), which the harness
    retries with other settings; but when the engine answers a trivial prompt
    with the same repeated symbol, no setting helps and the whole turn --
    every retry, then the fallback -- is lost. True when it restarted the
    engine and it came back healthy and sane. Never raises."""
    try:
        engine = restartable_engine_for_url(url)
        if engine is None:
            return False
        if await generates_sanely(url, model) is not False:
            return False
        from src import engines
        logger.warning("[engine-swap] engine %s answers with garbage; restarting it", engine.get("id"))
        await engines.stop_engine(engine["id"])
        await asyncio.sleep(_RESTART_PAUSE_S)
        result = await _do_start_and_wait(engine["id"], float(_settings()["autostart_timeout_s"]))
        if result.get("action") != "started":
            return False
        return bool(await generates_sanely(url, model))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[engine-swap] garbled-engine restart failed for %s: %s", url, exc)
        return False


async def ensure_ready(url: str, *, timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """If `url` maps to a managed engine that is not already healthy, start
    it (single-flight across concurrent callers) and wait for health.
    Never raises: any failure here is logged and returned, not thrown, so a
    problem in engine-swap never breaks a chat call — the real request will
    fail on its own with a clear connection error."""
    try:
        engine = engine_for_url(url)
        if engine is None:
            return {"action": "none"}
        cfg = _settings()
        if not cfg["autostart"]:
            return {"action": "none", "engine_id": engine["id"]}
        state = await _probe(engine)
        if state == "ok":
            touch(engine["id"])
            return {"action": "none", "engine_id": engine["id"]}

        engine_id = engine["id"]
        effective_timeout = float(timeout_s) if timeout_s is not None else cfg["autostart_timeout_s"]
        if state == "loading" and engine_id not in _starts:
            # Already starting (by us earlier, a script or the Settings
            # button): starting again would hit the busy port. Just wait.
            if await _wait_healthy(engine, effective_timeout):
                touch(engine_id)
                return {"action": "waited", "engine_id": engine_id}
            return {"action": "failed", "engine_id": engine_id, "error": "engine stayed in loading state"}
        task = _starts.get(engine_id)
        if task is None or task.done():
            task = asyncio.ensure_future(_do_start_and_wait(engine_id, effective_timeout))
            _starts[engine_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if _starts.get(engine_id) is task and task.done():
                _starts.pop(engine_id, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[engine-swap] ensure_ready failed for %s: %s", url, exc)
        return {"action": "failed", "error": str(exc)}


# ── idle reaper ──────────────────────────────────────────────────────────────

async def _reap_once() -> None:
    cfg = _settings()
    ttl_minutes = cfg["idle_ttl_minutes"]
    if ttl_minutes <= 0:
        return
    try:
        from src import engines
        all_engines = engines.list_engines()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[engine-swap] reaper: list_engines failed: %s", exc)
        return

    now = time.monotonic()
    ttl_s = ttl_minutes * 60.0
    for engine in all_engines:
        engine_id = engine.get("id")
        if not engine_id:
            continue
        if _in_flight.get(engine_id, 0) > 0:
            continue
        if not await _probe_healthy(engine):
            # Not running — nothing to reap, and forget any stale bookkeeping
            # so a later start gets a fresh idle clock.
            _first_seen_running.pop(engine_id, None)
            continue
        last = _last_used_mono.get(engine_id)
        if last is None:
            # Never touched by this process: start counting from now instead
            # of treating it as freshly used, so an externally-started
            # engine is still reaped eventually.
            last = _first_seen_running.setdefault(engine_id, now)
        idle_s = now - last
        if idle_s < ttl_s:
            continue
        logger.info("[engine-swap] unloaded idle engine %s (idle %.0fs >= ttl %.0fs)",
                    engine_id, idle_s, ttl_s)
        try:
            await engines.stop_engine(engine_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[engine-swap] failed to stop idle engine %s: %s", engine_id, exc)
            continue
        _last_used_mono.pop(engine_id, None)
        _last_used_wall.pop(engine_id, None)
        _first_seen_running.pop(engine_id, None)


async def _loop() -> None:
    await asyncio.sleep(3)
    while True:
        cfg = _settings()
        try:
            await _reap_once()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[engine-swap] reaper cycle failed: %s", exc)
        await asyncio.sleep(max(5.0, cfg["idle_check_s"]))


def start() -> None:
    """Spawn the idle reaper loop (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_loop(), name="engine-swap-reaper")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None


def status() -> Dict[str, Any]:
    cfg = _settings()
    now = time.monotonic()
    try:
        from src import engines
        all_engines = engines.list_engines()
    except Exception:  # noqa: BLE001
        all_engines = []
    per_engine: Dict[str, Any] = {}
    for engine in all_engines:
        engine_id = engine.get("id")
        if not engine_id:
            continue
        last = _last_used_mono.get(engine_id)
        per_engine[engine_id] = {
            "last_used": _last_used_wall.get(engine_id),
            "in_flight": _in_flight.get(engine_id, 0),
            "idle_s": (now - last) if last is not None else None,
        }
    return {
        "autostart": cfg["autostart"],
        "autostart_timeout_s": cfg["autostart_timeout_s"],
        "idle_ttl_minutes": cfg["idle_ttl_minutes"],
        "idle_check_s": cfg["idle_check_s"],
        "engines": per_engine,
    }
