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
        if not root:
            return None
        loaded = vram_admission._get(root, "/api/ps", 2.5).get("models") or []
        return [str(m.get("name") or m.get("model") or "") for m in loaded if m]
    except Exception:
        return None


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
