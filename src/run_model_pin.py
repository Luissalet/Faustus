"""Keep the local Ollama model resident for the length of an agent run.

A bash command that prints nothing for minutes used to let Ollama's saved
``keep_alive`` (often ``5m``) expire mid-turn: the weight unloaded, the usage
pill said "no model", and the next round paid a full reload. Pinning raises
``keep_alive`` on every request the run makes *for its own model*, then
restores the saved value when the run ends.

Design notes (each one cost a bug before it was written down):

* One pin per ``stream_agent_loop`` invocation, keyed by a fresh token — not
  by session id. A worker spawned inside the run shares the session id; if it
  unpinned by session id its ``finally`` would drop the parent's pin while the
  parent was still in its bash.
* The override only applies to the pinned ``model``. A judge, router or
  embedding model called during the run keeps its own saved ``keep_alive``;
  otherwise every model touched in a two-hour run stayed resident for two
  hours and the VRAM admission had nothing left to admit.
* The restore ping is a blocking HTTP call. It runs through
  :func:`unpin_for_run_async` (``asyncio.to_thread``) from the loop; the sync
  :func:`unpin_for_run` exists for tests and non-async callers only.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import uuid
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PINS: Dict[str, Dict[str, str]] = {}
_ACTIVE_PIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "faustus_run_model_pin", default=""
)


def reset_for_tests() -> None:
    with _LOCK:
        _PINS.clear()
    _ACTIVE_PIN.set("")


def _norm_model(model: str) -> str:
    m = str(model or "").strip().lower()
    # `qwen3:27b` and `qwen3:27b-q4_K_M` are different weights; `:latest` is
    # the same weight as the bare name.
    return m[:-7] if m.endswith(":latest") else m


def pin_for_run(run_id: str, endpoint: str, model: str) -> Optional[contextvars.Token]:
    """Register a pin for the current task context. Returns the contextvar
    token the caller must hand back to :func:`unpin_for_run`.

    ``run_id`` is informational (logs / introspection); the pin key is a
    fresh token so nested runs under one session never unpin each other.
    """
    if not str(model or "").strip():
        return None
    key = f"{str(run_id or '').strip() or 'run'}:{uuid.uuid4().hex[:8]}"
    with _LOCK:
        _PINS[key] = {
            "run_id": str(run_id or ""),
            "endpoint": str(endpoint or ""),
            "model": str(model or ""),
        }
    token = _ACTIVE_PIN.set(key)
    return token


def active_pin() -> Optional[Dict[str, str]]:
    key = _ACTIVE_PIN.get()
    if not key:
        return None
    with _LOCK:
        rec = _PINS.get(key)
    return dict(rec) if rec else None


def _pop_active(token: Optional[contextvars.Token]) -> Optional[Dict[str, str]]:
    key = _ACTIVE_PIN.get()
    with _LOCK:
        rec = _PINS.pop(key, None) if key else None
    if token is not None:
        try:
            _ACTIVE_PIN.reset(token)
        except Exception:  # noqa: BLE001 - token from another context
            _ACTIVE_PIN.set("")
    else:
        _ACTIVE_PIN.set("")
    return rec


def _restore_wanted() -> bool:
    try:
        from src.settings import get_setting
        return bool(get_setting("agent_run_keep_alive_restore", True))
    except Exception:  # noqa: BLE001
        return True


def unpin_for_run(run_id: str = "", token: Optional[contextvars.Token] = None) -> Optional[Dict[str, str]]:
    """Sync unpin + restore. Blocks up to 3 s on the restore ping — use
    :func:`unpin_for_run_async` from the event loop."""
    rec = _pop_active(token)
    if not rec:
        return None
    if _restore_wanted():
        ka = _saved_keep_alive(rec.get("endpoint") or "", rec.get("model") or "")
        restore_keep_alive(rec.get("endpoint") or "", rec.get("model") or "", ka)
    return rec


async def unpin_for_run_async(run_id: str = "", token: Optional[contextvars.Token] = None) -> Optional[Dict[str, str]]:
    """Unpin now (so a request racing the end of the run sees no pin) and
    ping Ollama off the event loop."""
    rec = _pop_active(token)
    if not rec:
        return None
    if _restore_wanted():
        endpoint = rec.get("endpoint") or ""
        model = rec.get("model") or ""
        try:
            await asyncio.to_thread(
                lambda: restore_keep_alive(endpoint, model, _saved_keep_alive(endpoint, model))
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("restore keep_alive (async) failed: %s", e)
    return rec


def keep_alive_override(model: str = "") -> Optional[str]:
    """The pinned keep_alive for ``model`` in the current context, or None.

    Without ``model`` the pin applies (legacy callers); with it, only when the
    request is for the pinned weight.
    """
    rec = active_pin()
    if not rec:
        return None
    if model and _norm_model(model) != _norm_model(rec.get("model") or ""):
        return None
    try:
        from src.settings import get_setting
        val = get_setting("agent_run_keep_alive", "2h")
    except Exception:  # noqa: BLE001
        val = "2h"
    val = str(val or "2h").strip()
    return val or "2h"


def _saved_keep_alive(endpoint: str, model: str) -> str:
    try:
        from src.model_load_options import resolve_for_request
        opts = resolve_for_request(endpoint, model) or {}
        ka = opts.get("keep_alive")
        if ka not in (None, ""):
            return str(ka)
    except Exception:  # noqa: BLE001
        pass
    return "5m"


def _root(endpoint: str) -> str:
    parsed = urlparse((endpoint or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (endpoint or "").rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _looks_like_ollama(endpoint: str) -> bool:
    try:
        from src.llm_core import _is_declared_ollama_host, _is_ollama_openai_compat_url
        return bool(_is_ollama_openai_compat_url(endpoint) or _is_declared_ollama_host(endpoint))
    except Exception:  # noqa: BLE001
        return "11434" in str(endpoint or "")


def restore_keep_alive(endpoint: str, model: str, keep_alive: str) -> bool:
    """Best-effort ping so Ollama drops back to the saved keep_alive. Never
    raises; never talks to a non-Ollama endpoint (OpenRouter has no
    /api/generate and no weight to keep)."""
    if not endpoint or not model or not _looks_like_ollama(endpoint):
        return False
    url = _root(endpoint) + "/api/generate"
    try:
        import httpx
        httpx.post(
            url,
            json={"model": model, "keep_alive": keep_alive, "prompt": "", "stream": False},
            timeout=3.0,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("restore_keep_alive failed: %s", e)
        return False
