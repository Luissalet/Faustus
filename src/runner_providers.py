"""src/runner_providers.py — residency probing beyond Ollama.

Every place that answers "what model is loaded" used to ask Ollama and only
Ollama (`/api/ps`, `/api/tags`, `/api/show`): `src/model_warmup.py`,
`src/vram_admission.py`, `routes/local_models_routes.py`,
`routes/system_usage_routes.py`. A model served by a self-hosted OpenAI-
compatible runner — llama.cpp's `llama-server`, registered as an ordinary
model endpoint — was invisible everywhere: not in "Loaded now", not in the
local models list, and the VRAM widget's model label said "no model" even
while the runner held the whole card.

This module is that runner's read side: a probe that answers the same three
questions Ollama's `/api/ps` answers for its own models — is something
loaded, what is it, how big — using the endpoints `llama-server` actually
exposes (see module docstring in the audit note this file was written
against):

  GET /health    → {"status": "ok"}  when a model is loaded (503 while loading)
  GET /v1/models → OpenAI-style {"data": [{"id": "<alias>"}]}
  GET /props     → model_path, n_ctx, default_generation_settings
  GET /slots     → per-slot state, including is_processing

There is no VRAM figure in that API and llama-server has no unload call, so
this module never invents either: the footprint comes from the GGUF file
size at `model_path` when that path is readable from this process (else
`footprint_bytes` is `None` and `footprint_measured` is `False`), and every
caller of `probe_llama_cpp` is expected to render "served by <endpoint>"
with no unload control rather than a button that would lie.

Reads are cached for a couple of seconds, the same way `assess()`'s /api/ps
read effectively is via `vram_admission`'s reservation bookkeeping — several
callers (the usage poller, the local-models page, admission) can all ask
inside the same tick without each paying a fresh HTTP round trip. Every
network call has a short timeout and every failure degrades to "unknown"
(`available: False`); nothing here ever raises or blocks a request.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_CACHE_TTL = 2.0
_cache: Dict[str, Dict[str, Any]] = {}  # root -> {"at": ts, "data": {...}}

_PROBE_TIMEOUT = 1.5

# Same collapsing every other local-probe module does (model_warmup.py,
# vram_admission.py): a footprint measured from a GGUF file, and a VRAM
# reading taken from nvidia-smi, are only honest for a runner on THIS
# machine — a LAN or tailnet server gets no such claim.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def is_same_machine(root: str) -> bool:
    try:
        return (urlparse(root or "").hostname or "").lower() in _LOCAL_HOSTS
    except ValueError:
        return False


def clear_cache() -> None:
    """Test hook: drop every cached probe reading."""
    _cache.clear()


def root_from_base(base_url: str) -> Optional[str]:
    """``http://127.0.0.1:8081/v1`` → ``http://127.0.0.1:8081``. `None` for
    anything that is not a plain http(s) URL."""
    url = str(base_url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if not p.hostname:
        return None
    port = f":{p.port}" if p.port else ""
    return f"{p.scheme}://{p.hostname}{port}"


def _get_json(url: str, timeout: float) -> Optional[Any]:
    import httpx
    from src.tls_overrides import llm_verify
    try:
        r = httpx.get(url, timeout=timeout, verify=llm_verify())
        if r.status_code >= 400 and r.status_code != 503:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    except Exception as e:  # noqa: BLE001 - a probe never raises into the caller
        logger.debug("runner_providers: GET %s failed: %s", url, e)
        return None


def _gguf_footprint(model_path: str) -> Optional[int]:
    """The on-disk size of the GGUF this runner loaded, when the path is one
    this process can actually read (same machine, readable permissions).
    Never guessed: no path, no file, no size — `None`, not a zero."""
    path = str(model_path or "").strip()
    if not path:
        return None
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
    except OSError as e:  # noqa: BLE001
        logger.debug("runner_providers: stat %s failed: %s", path, e)
    return None


def _empty(available: bool = False) -> Dict[str, Any]:
    return {
        "available": available,
        "healthy": False,
        "model": "",
        "context_length": 0,
        "footprint_bytes": None,
        "footprint_measured": False,
        "generating": False,
    }


def probe_llama_cpp(root: str, *, timeout: float = _PROBE_TIMEOUT, force: bool = False) -> Dict[str, Any]:
    """What a self-hosted OpenAI-compatible runner at `root` (scheme://host:port,
    no trailing path) has resident right now.

    Never raises. `available` is False whenever `/health` did not answer at
    all (nothing there, or not this kind of server) — every other key then
    holds its empty default so a caller can destructure without checking.
    `healthy` distinguishes "answered 503, still loading" from "answered ok".
    """
    root = str(root or "").rstrip("/")
    if not root:
        return _empty()
    now = time.time()
    cached = _cache.get(root)
    if not force and cached is not None and now - cached["at"] < _CACHE_TTL:
        return dict(cached["data"])

    out = _empty()
    health = _get_json(root + "/health", timeout)
    if health is None:
        _cache[root] = {"at": now, "data": out}
        return dict(out)
    out["available"] = True
    out["healthy"] = isinstance(health, dict) and str(health.get("status") or "").lower() == "ok"

    models = _get_json(root + "/v1/models", timeout)
    if isinstance(models, dict):
        rows = models.get("data") or []
        if rows and isinstance(rows[0], dict):
            out["model"] = str(rows[0].get("id") or "")

    props = _get_json(root + "/props", timeout)
    if isinstance(props, dict):
        try:
            out["context_length"] = int(props.get("n_ctx") or 0)
        except (TypeError, ValueError):
            out["context_length"] = 0
        size = _gguf_footprint(str(props.get("model_path") or ""))
        if size:
            out["footprint_bytes"] = size
            out["footprint_measured"] = True

    slots = _get_json(root + "/slots", timeout)
    rows: List[Any] = []
    if isinstance(slots, list):
        rows = slots
    elif isinstance(slots, dict) and isinstance(slots.get("slots"), list):
        rows = slots["slots"]
    out["generating"] = any(isinstance(s, dict) and bool(s.get("is_processing")) for s in rows)

    _cache[root] = {"at": now, "data": out}
    return dict(out)


# ── which configured endpoints are candidates ────────────────────────────────

def _is_ollama_root(base_url: str) -> bool:
    try:
        from routes.model_routes import _is_ollama_base
        return _is_ollama_base(base_url)
    except Exception:  # noqa: BLE001
        return False


def list_external_runner_endpoints(*, owner: str = "", is_admin: bool = True) -> List[Dict[str, Any]]:
    """Configured, enabled model endpoints that are self-hosted OpenAI-
    compatible servers and NOT Ollama — the population `probe_llama_cpp` is
    worth calling for. Same visibility rule as the Ollama listing: admins see
    everything, a regular user sees the shared endpoints plus their own."""
    from core.database import SessionLocal, ModelEndpoint
    from src.auth_helpers import owner_filter

    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            if owner and not is_admin:
                q = owner_filter(q, ModelEndpoint, owner)
            rows = q.all()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.debug("runner_providers: endpoint query failed: %s", e)
        return out
    for ep in rows:
        base = str(getattr(ep, "base_url", "") or "")
        if not base or _is_ollama_root(base):
            continue
        root = root_from_base(base)
        if not root or root in seen:
            continue
        seen.add(root)
        out.append({
            "id": str(getattr(ep, "id", "") or ""),
            "name": str(getattr(ep, "name", "") or "Local runner"),
            "base_url": base,
            "root": root,
        })
    return out


def external_runner_snapshot(*, owner: str = "", is_admin: bool = True,
                             timeout: float = _PROBE_TIMEOUT,
                             same_machine_only: bool = False) -> List[Dict[str, Any]]:
    """`probe_llama_cpp` for every configured non-Ollama endpoint that
    answers, each row tagged with the endpoint that serves it. Endpoints that
    do not answer `/health` at all (a cloud API, a dead server) are left out
    — this is a residency list, not an endpoint health check. `same_machine_
    only` restricts to endpoints this process can also read a GGUF file size
    and an nvidia-smi card for (vram_admission's use)."""
    out: List[Dict[str, Any]] = []
    for ep in list_external_runner_endpoints(owner=owner, is_admin=is_admin):
        if same_machine_only and not is_same_machine(ep["root"]):
            continue
        probe = probe_llama_cpp(ep["root"], timeout=timeout)
        if not probe.get("available") or not probe.get("model"):
            continue
        row = dict(probe)
        row["endpoint_id"] = ep["id"]
        row["endpoint_name"] = ep["name"]
        row["engine"] = "llama.cpp"
        row["unloadable"] = False
        out.append(row)
    return out
