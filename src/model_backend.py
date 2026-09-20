"""src/model_backend.py — who actually serves a given model endpoint.

Every screen that lists a local model (Settings → Default AI, Settings →
Local models, the chat model picker) shows the model's name and, sometimes,
its size or fit — but never *which server* answers the request. Two very
different local setups look identical: an Ollama-managed model and a
llama.cpp `llama-server` instance registered as a plain OpenAI-compatible
endpoint both just show up as "a local model". This module is the one place
that answers "who serves it", so every screen renders the same label instead
of five slightly different heuristics.

`serving_backend(base_url, endpoint_kind=...)` returns one of:

  * ``ollama``   — a native Ollama root (port 11434, or "ollama" in the
    hostname — the same heuristic `routes/model_routes.py::_is_ollama_base`
    and `src/model_warmup.py::_ollama_root` already use, kept in sync here
    rather than re-derived a fourth time).
  * ``llamacpp`` — either a URL that maps onto a `src.engines`-managed
    `llama-server` (checked via `src.engine_swap.engine_for_url`, so this
    never re-implements that match), or an unmanaged local OpenAI-compatible
    server that answers `GET /props` (llama-server's own endpoint; Ollama has
    no such route).
  * ``remote``   — anything not on a private/loopback address, or explicitly
    labelled `api`/`proxy` (`ModelEndpoint.endpoint_kind`).
  * ``unknown``  — a local address that answered neither probe (down, or a
    server this module does not recognise).

Detection for an *unlabelled* local endpoint needs one small probe
(`/api/version` for Ollama, `/props` for llama-server); that probe result is
cached for `_PROBE_TTL` seconds per root so a picker with a dozen rows pays
it once, not once per row per render. Every probe is short-timeout and
best-effort: a network hiccup degrades to "unknown", never an exception that
would break the screen rendering the badge.
"""
from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),  # Tailscale CGNAT
)

_PROBE_TTL = 30.0
_probe_cache: Dict[str, Dict[str, Any]] = {}  # root -> {"at": ts, "backend": str|None}

_LABELS = {
    "ollama": "Ollama",
    "llamacpp": "llama.cpp (llama-server)",
    "remote": "Remote API",
    "unknown": "Unknown",
}


def clear_cache() -> None:
    """Test/debug hook: drop every cached probe result."""
    _probe_cache.clear()


def is_ollama_url(url: str) -> bool:
    """Same test `src/model_warmup.py::_ollama_root` and
    `routes/model_routes.py::_is_ollama_base` use: port 11434 or "ollama" in
    the hostname."""
    try:
        p = urlparse(str(url or ""))
        host = (p.hostname or "").lower()
        return p.port == 11434 or "ollama" in host
    except Exception:  # noqa: BLE001
        return "ollama" in str(url or "").lower()


def _is_local_or_private(host: str) -> bool:
    h = (host or "").lower()
    if h in _LOCAL_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE_NETWORKS)


def _root(url: str) -> Optional[str]:
    try:
        p = urlparse(str(url or ""))
    except Exception:  # noqa: BLE001
        return None
    if not p.hostname:
        return None
    return f"{p.scheme or 'http'}://{p.hostname}:{p.port or 80}"


def _managed_llamacpp_engine(url: str) -> Optional[Dict[str, Any]]:
    try:
        from src import engine_swap
        return engine_swap.engine_for_url(url)
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_backend: engine_for_url failed for %s: %s", url, exc)
        return None


def _probe_local_backend(root: str) -> Optional[str]:
    """Best-effort disambiguation for an unlabelled local endpoint that is
    not a managed engine: try Ollama's native `/api/version`, then
    llama-server's `/props`. Cached per root; never raises."""
    now = time.time()
    cached = _probe_cache.get(root)
    if cached and (now - cached["at"]) < _PROBE_TTL:
        return cached.get("backend")
    backend: Optional[str] = None
    try:
        import httpx
        with httpx.Client(timeout=1.5, trust_env=False) as client:
            try:
                resp = client.get(root.rstrip("/") + "/api/version")
                if resp.status_code == 200:
                    backend = "ollama"
            except Exception as exc:  # noqa: BLE001
                logger.debug("model_backend: /api/version probe failed for %s: %s", root, exc)
            if backend is None:
                try:
                    resp = client.get(root.rstrip("/") + "/props")
                    if resp.status_code == 200:
                        backend = "llamacpp"
                except Exception as exc:  # noqa: BLE001
                    logger.debug("model_backend: /props probe failed for %s: %s", root, exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("model_backend: probe unavailable for %s: %s", root, exc)
    _probe_cache[root] = {"at": now, "backend": backend}
    return backend


def serving_backend(base_url: str, *, endpoint_kind: Optional[str] = None,
                     probe: bool = True) -> Dict[str, str]:
    """``{"backend": "ollama"|"llamacpp"|"remote"|"unknown", "label": ...}``
    for a model endpoint's base URL. Never raises; the probe (when allowed)
    is cached and short-timeout, so this is safe to call once per row in a
    listing endpoint."""
    url = str(base_url or "")
    kind = str(endpoint_kind or "auto").strip().lower()

    if is_ollama_url(url):
        return {"backend": "ollama", "label": _LABELS["ollama"]}

    engine = _managed_llamacpp_engine(url)
    if engine is not None:
        return {"backend": "llamacpp", "label": _LABELS["llamacpp"]}

    if kind in ("api", "proxy"):
        return {"backend": "remote", "label": _LABELS["remote"]}

    try:
        host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        host = ""
    local = kind == "local" or _is_local_or_private(host)
    if not local:
        return {"backend": "remote", "label": _LABELS["remote"]}

    root = _root(url)
    backend = _probe_local_backend(root) if (probe and root) else None
    backend = backend or "unknown"
    return {"backend": backend, "label": _LABELS.get(backend, _LABELS["unknown"])}
