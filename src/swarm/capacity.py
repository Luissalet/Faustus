"""src/swarm/capacity.py — how many swarm calls one endpoint can really serve
at once.

  * llama-server (local): its own ``/slots`` (one entry per ``-np`` slot) or
    ``/props`` ``total_slots`` -- the same payloads
    ``src.model_capability_readers.llamacpp`` already normalises into
    ``limits["parallel_slots"]``. Cached per server for a minute.
  * Ollama: ``OLLAMA_NUM_PARALLEL`` is an environment variable of the Ollama
    SERVER and no request can read it back (see the note in
    ``src/model_load_options.py``), so the owner states it in the setting
    ``swarm_ollama_parallel`` (default 1).
  * A remote API: ``swarm_api_parallel`` (default 8).
  * Anything local that answers neither: 1.

Whatever the source, the result is capped by ``swarm_max_parallel`` (16).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_API_PARALLEL = 8
DEFAULT_OLLAMA_PARALLEL = 1
DEFAULT_MAX_PARALLEL = 16
DEFAULT_MAX_ITEMS = 200
SLOT_CACHE_SECONDS = 60.0

_SLOT_CACHE: Dict[str, Tuple[float, Optional[int]]] = {}


def _setting(key: str, default: Any) -> Any:
    try:
        from src.settings import get_setting
        value = get_setting(key, default)
        return default if value is None else value
    except Exception:  # noqa: BLE001
        return default


def _int_setting(key: str, default: int, lo: int = 1, hi: int = 1024) -> int:
    try:
        value = int(_setting(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def max_parallel() -> int:
    return _int_setting("swarm_max_parallel", DEFAULT_MAX_PARALLEL, 1, 256)


def max_items() -> int:
    return _int_setting("swarm_max_items", DEFAULT_MAX_ITEMS, 1, 100000)


def server_root(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_local(url: str) -> bool:
    try:
        from src.model_context import is_local_endpoint
        return bool(is_local_endpoint(url))
    except Exception:  # noqa: BLE001
        host = (urlparse(url or "").hostname or "").lower()
        return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _looks_ollama(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return False
    return parsed.port == 11434 or "ollama" in (parsed.hostname or "").lower() or "/api/chat" in (parsed.path or "")


async def _get_json(url: str, timeout: float) -> Any:
    import httpx
    try:
        from src.tls_overrides import llm_verify
        verify = llm_verify()
    except Exception:  # noqa: BLE001
        verify = True
    async with httpx.AsyncClient(timeout=timeout, verify=verify) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def llamacpp_slots(url: str, *, refresh: bool = False, timeout: float = 2.5) -> Optional[int]:
    """Parallel slots of the llama-server behind `url`, or None when it is
    not one (or does not answer). ``/slots`` first (it is the per-slot list
    ``-np`` creates), ``/props`` ``total_slots`` when ``/slots`` is off."""
    root = server_root(url)
    if not root:
        return None
    now = time.monotonic()
    cached = _SLOT_CACHE.get(root)
    if cached is not None and not refresh and now - cached[0] < SLOT_CACHE_SECONDS:
        return cached[1]
    from src.model_capability_readers.llamacpp import _limits_from_props
    slots_payload: Any = None
    props_payload: Any = None
    try:
        slots_payload = await _get_json(root + "/slots", timeout)
    except Exception:  # noqa: BLE001 - --no-slots or not a llama-server
        slots_payload = None
    try:
        props_payload = await _get_json(root + "/props", timeout)
    except Exception:  # noqa: BLE001
        props_payload = None
    slots: Optional[int] = None
    if isinstance(props_payload, dict) or isinstance(slots_payload, list):
        limits = _limits_from_props(props_payload if isinstance(props_payload, dict) else {},
                                    slots_payload if isinstance(slots_payload, list) else None)
        value = limits.get("parallel_slots")
        slots = int(value) if value else None
    _SLOT_CACHE[root] = (now, slots)
    if isinstance(slots_payload, list):
        _BUSY_SEEN[root] = sum(1 for sl in slots_payload
                               if isinstance(sl, dict) and sl.get("is_processing"))
    return slots


_BUSY_SEEN: Dict[str, int] = {}


def busy_slots_seen(url: str) -> int:
    """Slots the server reported busy on the last `/slots` read (other chats,
    other instances). 0 when unknown."""
    return int(_BUSY_SEEN.get(server_root(url), 0) or 0)


async def effective_parallel(url: str, *, refresh: bool = False) -> Dict[str, Any]:
    """``{"parallel", "source", "backend", "local"}`` for `url` (uncapped by
    the item count -- the runner does that)."""
    cap = max_parallel()
    local = _is_local(url)
    if not local:
        n = _int_setting("swarm_api_parallel", DEFAULT_API_PARALLEL)
        return {"parallel": min(n, cap), "source": "setting:swarm_api_parallel",
                "backend": "api", "local": False}
    if _looks_ollama(url):
        n = _int_setting("swarm_ollama_parallel", DEFAULT_OLLAMA_PARALLEL)
        return {"parallel": min(n, cap), "source": "setting:swarm_ollama_parallel",
                "backend": "ollama", "local": True}
    slots = await llamacpp_slots(url, refresh=True)
    if slots:
        # Slots other conversations are using right now are not ours: taking
        # all four on a shared server queued every item behind them and
        # timed items out while they waited.
        busy = min(busy_slots_seen(url), int(slots) - 1)
        free = max(1, int(slots) - max(0, busy))
        return {"parallel": max(1, min(free, cap)), "source": "llamacpp:/slots",
                "backend": "llamacpp", "local": True, "slots": int(slots), "busy": busy}
    return {"parallel": 1, "source": "local_default", "backend": "local", "local": True}
