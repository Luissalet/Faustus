"""Stops an unattended job from loading a big model on its own.

Real incident: at 02:00, `routes/skills_routes.py`'s nightly skill audit
logged "Scheduled skill audit starting: 7 skill(s) (owner=all)" and
immediately made a non-streaming call that loaded `deepseek-r1:32b` (37.5 GB)
on the local Ollama runner, while the owner's main model was serving a long
task on another engine. Two separate problems, two separate fixes:

1. The audit must resolve its model through the same utility/task settings
   chain every other background job uses (`src.task_endpoint`,
   `src.endpoint_resolver.resolve_endpoint`) — never guess "first available"
   when the configured model doesn't match what the endpoint reports
   (`routes/skills_routes.py::_resolve_audit_models`, fixed separately).

2. Even a CORRECTLY resolved model must not be loaded into VRAM by an
   unattended job. This module is that second line of defense: the choke
   point every scheduled/background job should call right before it spends a
   model call.
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def _resident_model_names(url: str) -> Optional[List[str]]:
    """Model names currently loaded on the local Ollama runner behind `url`.

    Returns None when residency cannot be determined at all: `url` is not a
    local Ollama endpoint (a remote provider, or a self-hosted server with no
    `/api/ps`, e.g. llama-server) or the runner could not be reached. None is
    deliberately NOT treated as "nothing loaded" by the caller — see
    `would_require_load`.
    """
    try:
        from src import vram_admission
        root = vram_admission.ollama_root(url)
    except Exception:
        return None
    if root:
        try:
            loaded = vram_admission._get(root, "/api/ps", 2.5).get("models") or []
            return [str(m.get("name") or m.get("model") or "") for m in loaded if m]
        except Exception:
            return None
    loopback = _loopback_root(url)
    if not loopback:
        return None
    return _llama_server_models(vram_admission, loopback)


def _loopback_root(url: str) -> Optional[str]:
    """`http://127.0.0.1:8082/v1/chat/completions` -> `http://127.0.0.1:8082`,
    only for a server on this machine (never probe a remote provider)."""
    from urllib.parse import urlparse
    try:
        p = urlparse(str(url or "").strip())
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or host not in ("127.0.0.1", "localhost", "::1") or not p.port:
        return None
    shown = f"[{host}]" if ":" in host else host
    return f"{p.scheme}://{shown}:{p.port}"


def _llama_server_models(vram_admission, root: str) -> Optional[List[str]]:
    """Residency on a loopback llama-server (no `/api/ps`).

    A llama-server that answers `/health` with "ok" has its model(s) in memory:
    it loads before it listens. In router mode each `/v1/models` entry carries
    a `status` and only the "loaded" ones count. Anything else — not a
    llama-server, still loading, unreachable — stays None ("cannot tell"),
    which the callers treat as "would load"."""
    try:
        health = vram_admission._get(root, "/health", 2.5)
        if str((health or {}).get("status") or "").lower() != "ok":
            return None
        data = vram_admission._get(root, "/v1/models", 2.5).get("data") or []
    except Exception:
        return None
    names: List[str] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        if isinstance(status, dict):
            status = status.get("value")
        if status not in (None, "", "loaded"):
            continue
        name = str(entry.get("id") or entry.get("model") or "").strip()
        if name:
            names.append(name)
    return names or None


def model_busy(url: str) -> bool:
    """True when a loopback llama-server behind `url` is serving a request
    right now (`/slots` reports a slot processing). A single-slot server
    shared with a person's chat must not get a background request queued in
    front of them. Ollama and anything that cannot answer: False."""
    root = _loopback_root(url)
    if not root:
        return False
    try:
        from src import vram_admission
        slots = vram_admission._get(root, "/slots", 1.5)
    except Exception:
        return False
    if isinstance(slots, dict):
        slots = slots.get("slots") or []
    return any(isinstance(s, dict) and s.get("is_processing") for s in (slots or []))


def would_require_load(url: str, model: str) -> bool:
    """True if calling `model` at `url` right now would make Ollama load it
    fresh (or if we simply cannot tell — see `_resident_model_names`).

    An endpoint we cannot check (remote provider, non-Ollama local server,
    `/api/ps` unreachable) is treated as "would require a load": a background
    job must not gamble on an unattended VRAM swap just because it could not
    confirm residency either way.
    """
    if not url or not model:
        return True
    names = _resident_model_names(url)
    if names is None:
        return True
    from src.llm_core import _same_model_identity
    return not any(_same_model_identity(n, model) for n in names if n)


def should_run_with_model(job_name: str, url: str, model: str, *, user_initiated: bool = False) -> bool:
    """The guard: call this right before a scheduled/background job spends
    its resolved model call. Returns False when the run should be skipped.

    - `user_initiated=True` (a person clicked a button) always proceeds —
      this guard exists for work nobody is watching, not for the person who
      just asked for it.
    - The `background_jobs_may_load_models` setting (default False) is the
      owner's explicit opt-in to let unattended jobs load models; when set,
      this always proceeds too.
    - Otherwise, a model already resident on the local runner is free to use
      (no load happens) and the job proceeds; a model that would need
      loading is postponed, logged as a single line, never loaded.
    """
    if user_initiated:
        return True
    try:
        from src.settings import get_setting
        if get_setting("background_jobs_may_load_models", False):
            return True
    except Exception:
        pass
    if not would_require_load(url, model):
        return True
    logger.info(f"scheduled {job_name}: postponed, would load {model}")
    return False
