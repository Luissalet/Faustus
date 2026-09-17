"""src/model_warmup.py — load the default chat model at startup and keep it loaded.

A cold local model means the first answer of the day waits a minute for the
weights (seen live: 'Ask Faustus' sat on 'Thinking…' while a 27B loaded).
When the default chat endpoint is a local Ollama, this module asks Ollama to
load that model right after the server is up — `POST /api/generate` with no
prompt loads without generating — with `keep_alive: -1`, i.e. "do not
unload", and re-issues the same request every few minutes so a later chat
that passed its own shorter keep_alive (the agent-run pin, a per-model
default) does not let it fall out of memory. Remote endpoints have nothing
to warm. Everything here is best effort and never blocks startup.

Settings: `warm_default_model` (True), `warm_default_model_keep_alive`
("-1" = forever; any Ollama duration works), `warm_default_model_every_s`
(120): another app talking to the same Ollama with its own default keep_alive (5 m) unpins the model, so the pin is renewed often.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_last: Dict[str, Any] = {"model": None, "url": None, "at": None, "ok": None, "detail": ""}


def _settings() -> Dict[str, Any]:
    try:
        from src.settings import get_setting
        keep = get_setting("warm_default_model_keep_alive", "-1")
        return {
            "enabled": bool(get_setting("warm_default_model", True)),
            "keep_alive": keep if keep not in (None, "") else "-1",
            "every_s": float(get_setting("warm_default_model_every_s", 120) or 120),
        }
    except Exception:  # noqa: BLE001
        return {"enabled": True, "keep_alive": "-1", "every_s": 120.0}


def _keep_alive_value(raw: Any) -> Any:
    """Ollama wants a number (seconds; -1 = forever) or a duration string."""
    s = str(raw).strip()
    try:
        return int(s)
    except ValueError:
        return s or -1


def _ollama_root(url: str) -> Optional[str]:
    """`http://host:port` when `url` is a local Ollama chat endpoint (native
    `/api/chat` or the OpenAI-compatible `/v1`), else None."""
    try:
        p = urlparse(url or "")
    except Exception:  # noqa: BLE001
        return None
    host = p.hostname or ""
    local = host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or p.port == 11434
    path = (p.path or "").rstrip("/")
    if not local or not (path in ("", "/v1", "/api") or path.startswith("/v1/") or path.startswith("/api/")):
        return None
    return f"{p.scheme or 'http'}://{host}:{p.port or 11434}"


def resolve_default() -> Optional[Dict[str, str]]:
    """The default chat model when it lives on a local Ollama: {url, model, root}."""
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, _headers = resolve_endpoint("default")
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: default endpoint unresolved: %s", exc)
        return None
    if not url or not model:
        return None
    root = _ollama_root(url)
    if not root:
        return None
    return {"url": url, "model": model, "root": root}


def _load_options(url: str, model: str) -> Dict[str, Any]:
    """The per-model load options Settings → Local models saved, flattened
    like `routes/local_models_routes._set_keep_alive` does (named knobs win
    over `extra`); keep_alive is carried by the request itself."""
    try:
        from src.llm_core import _model_load_defaults
        saved = dict(_model_load_defaults(url, model) or {})
    except Exception as exc:  # noqa: BLE001
        logger.debug("model warmup: load options unavailable: %s", exc)
        return {}
    opts = {k: v for k, v in saved.items() if k not in ("keep_alive", "extra") and v not in (None, "")}
    extra = saved.get("extra")
    if isinstance(extra, dict):
        opts = {**extra, **opts}
    return opts


async def warm_once() -> Dict[str, Any]:
    """Ask Ollama to load the default model (no generation) and pin it."""
    cfg = _settings()
    target = resolve_default()
    if not target:
        _last.update({"ok": None, "detail": "no local default model"})
        return dict(_last)
    import httpx
    import time
    body = {"model": target["model"], "keep_alive": _keep_alive_value(cfg["keep_alive"])}
    # Build the runner the way the chats will ask for it: the saved load
    # options (num_ctx, num_gpu, main_gpu, extra). Without them the model
    # came up with Ollama's default context and the first real prompt made
    # Ollama tear it down and load it again — the wait this module exists
    # to remove.
    load_opts = _load_options(target["url"], target["model"])
    if load_opts:
        body["options"] = load_opts
    try:
        async with httpx.AsyncClient(timeout=300.0, trust_env=False) as client:
            resp = await client.post(target["root"] + "/api/generate", json=body)
        ok = resp.status_code == 200
        detail = "" if ok else resp.text[:200]
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"
    _last.update({"model": target["model"], "url": target["root"], "at": time.time(), "ok": ok, "detail": detail})
    (logger.info if ok else logger.warning)("model warmup: %s %s%s", target["model"], "loaded" if ok else "failed", f" ({detail})" if detail else "")
    return dict(_last)


async def _loop() -> None:
    # Give the rest of startup (MCP, indexes) a head start; the model load
    # is the heavy one and nothing else needs the GPU yet.
    await asyncio.sleep(3)
    while True:
        cfg = _settings()
        if cfg["enabled"]:
            try:
                await warm_once()
            except Exception as exc:  # noqa: BLE001
                logger.debug("model warmup failed: %s", exc)
        await asyncio.sleep(max(60.0, cfg["every_s"]))


def start() -> None:
    """Spawn the warmup loop (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_loop(), name="model-warmup")


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
    return {**_settings(), **_last}
