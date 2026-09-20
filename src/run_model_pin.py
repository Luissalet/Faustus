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

import re

import asyncio
import contextvars
import logging
import threading
import uuid
from typing import Any, Dict, Optional
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


def _saved_keep_alive(endpoint: str, model: str) -> Any:
    try:
        from src.model_load_options import resolve_for_request
        opts = resolve_for_request(endpoint, model) or {}
        ka = opts.get("keep_alive")
        if ka not in (None, ""):
            # A bare number ("-1" = forever, 600) must reach Ollama as a
            # number: its duration parser rejects the text with HTTP 400
            # (seen live after every turn).
            text = str(ka).strip()
            return int(text) if re.fullmatch(r"-?\d+", text) else text
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


def _is_default_model(endpoint: str, model: str) -> bool:
    """The owner's rule: whatever Settings → Default AI names must never be
    handed a keep_alive shorter than forever by THIS code path — an agent run
    ending is routine, not a reason to let the residency keeper's job lapse
    even for the few seconds until its next cycle.

    Delegates to `src.model_warmup.is_default` (X-D: the shared helper
    `src.vram_admission.is_default_model` also uses) instead of re-deriving
    the comparison here."""
    try:
        from src import model_warmup
        return model_warmup.is_default(endpoint, model)
    except Exception:  # noqa: BLE001
        return False


def _resident_load_options(ps: Dict[str, Any], endpoint: str, model: str) -> Dict[str, Any]:
    """Load-time options that make a keep_alive ping match the runner that is
    already resident: its real context length from /api/ps, plus the saved
    num_gpu/main_gpu for the model. Empty when nothing is known."""
    options: Dict[str, Any] = {}
    want = _norm_model(model)
    for entry in (ps.get("models") or []):
        if not isinstance(entry, dict):
            continue
        if _norm_model(str(entry.get("name") or entry.get("model") or "")) != want:
            continue
        try:
            ctx = int(entry.get("context_length") or 0)
        except (TypeError, ValueError):
            ctx = 0
        if ctx > 0:
            options["num_ctx"] = ctx
        break
    try:
        from src.model_load_options import resolve_for_request
        saved = resolve_for_request(endpoint, model) or {}
    except Exception:  # noqa: BLE001
        saved = {}
    if "num_ctx" not in options and saved.get("num_ctx"):
        options["num_ctx"] = saved["num_ctx"]
    for key in ("num_gpu", "main_gpu"):
        if saved.get(key) not in (None, ""):
            options[key] = saved[key]
    return options


def restore_keep_alive(endpoint: str, model: str, keep_alive: Any) -> bool:
    """Best-effort ping so Ollama drops back to the saved keep_alive. Never
    raises; never talks to a non-Ollama endpoint (OpenRouter has no
    /api/generate and no weight to keep); never sends the default model a
    keep_alive shorter than forever (skips a shorter one entirely — no ping
    at all needed since it is already pinned by the residency keeper)."""
    if not endpoint or not model or not _looks_like_ollama(endpoint):
        return False
    if _is_default_model(endpoint, model):
        ka_text = str(keep_alive).strip()
        if ka_text not in ("-1", "-1.0"):
            logger.debug("restore_keep_alive: %s is the default model — keeping it pinned at -1 instead of %r",
                         model, keep_alive)
            keep_alive = -1
    url = _root(endpoint) + "/api/generate"
    try:
        import httpx
        # An empty-prompt generate LOADS the model when it is not resident
        # (that is Ollama's documented "load" call). Only touch a model that
        # is actually resident; otherwise there is no keep_alive to restore
        # and the ping would pull 18 GB back into VRAM for nothing.
        try:
            ps = httpx.get(_root(endpoint) + "/api/ps", timeout=3.0).json() or {}
        except Exception:  # noqa: BLE001
            ps = {}
        resident = {_norm_model(str(m.get("name") or m.get("model") or ""))
                    for m in (ps.get("models") or []) if isinstance(m, dict)}
        if resident and _norm_model(model) not in resident:
            return False
        if not resident and ps:
            return False
        body: Dict[str, Any] = {"model": model, "keep_alive": keep_alive, "prompt": "", "stream": False}
        # 20-09-2026: a bare ping carries Ollama's default num_ctx. When the
        # runner was loaded with another window (200k here) Ollama treats the
        # ping as a reload: it tears the resident runner down, starts a new
        # one, and the 3 s timeout below closes the connection mid-load
        # ("client connection closed ... aborting load") — every run end
        # evicted the 27B. Echo the resident runner's own context so the
        # ping only moves the keep_alive.
        options = _resident_load_options(ps, endpoint, model)
        if options:
            body["options"] = options
        httpx.post(url, json=body, timeout=3.0)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("restore_keep_alive failed: %s", e)
        return False
