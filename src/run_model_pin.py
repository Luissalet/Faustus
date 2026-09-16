"""Keep the local Ollama model resident for the length of an agent run.

A bash command that prints nothing for minutes used to let Ollama's saved
``keep_alive`` (often ``5m``) expire mid-turn. Pinning raises ``keep_alive``
on every request of the run, then restores the saved value when the run ends.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PINS: Dict[str, Dict[str, str]] = {}
_ACTIVE_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "faustus_run_model_pin", default=""
)


def reset_for_tests() -> None:
    with _LOCK:
        _PINS.clear()
    _ACTIVE_RUN_ID.set("")


def pin_for_run(run_id: str, endpoint: str, model: str) -> Optional[contextvars.Token]:
    rid = str(run_id or "").strip()
    if not rid:
        return None
    with _LOCK:
        _PINS[rid] = {"endpoint": str(endpoint or ""), "model": str(model or "")}
    return _ACTIVE_RUN_ID.set(rid)


def unpin_for_run(run_id: str, token: Optional[contextvars.Token] = None) -> Optional[Dict[str, str]]:
    rid = str(run_id or "").strip()
    with _LOCK:
        rec = _PINS.pop(rid, None)
    if token is not None:
        try:
            _ACTIVE_RUN_ID.reset(token)
        except Exception:  # noqa: BLE001
            _ACTIVE_RUN_ID.set("")
    elif _ACTIVE_RUN_ID.get() == rid:
        _ACTIVE_RUN_ID.set("")
    if not rec:
        return None
    try:
        from src.settings import get_setting
        restore = bool(get_setting("agent_run_keep_alive_restore", True))
    except Exception:  # noqa: BLE001
        restore = True
    if restore:
        ka = _saved_keep_alive(rec.get("endpoint") or "", rec.get("model") or "")
        restore_keep_alive(rec.get("endpoint") or "", rec.get("model") or "", ka)
    return rec


def keep_alive_override(run_id: Optional[str] = None) -> Optional[str]:
    rid = str(run_id or _ACTIVE_RUN_ID.get() or "").strip()
    if not rid:
        return None
    with _LOCK:
        if rid not in _PINS:
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


def restore_keep_alive(endpoint: str, model: str, keep_alive: str) -> bool:
    """Best-effort ping so Ollama drops back to the saved keep_alive. Never raises."""
    if not endpoint or not model:
        return False
    url = _root(endpoint) + "/api/generate"
    try:
        import httpx
        httpx.post(
            url,
            json={
                "model": model,
                "keep_alive": keep_alive,
                "prompt": " ",
                "stream": False,
            },
            timeout=3.0,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("restore_keep_alive failed: %s", e)
        return False
