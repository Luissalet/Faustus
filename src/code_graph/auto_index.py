"""src/code_graph/auto_index.py — background auto-index for a code turn.

`maybe_auto_index(workspace)` is the one-line hook `agent_loop.py` calls
right where it already decides "this turn has a bound workspace and is a
coding turn" (`if workspace and not suppress_local_context:` /
`_workspace_coding_rules(workspace)`). Setting-gated
(`agent_code_graph_auto_index`, default True) and self-throttled — a
workspace already indexed within `_STALE_AFTER_S` is left alone — so this
never does real work on most turns and never blocks the turn on the one it
does: the actual index build runs in a background thread
(`asyncio.to_thread`) fired with `asyncio.create_task` and never awaited
here. Every failure mode (no running event loop, code_index unavailable,
sqlite error) is swallowed — auto-index is a convenience, never a turn
dependency.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

#: An index built in the last 5 minutes is fresh enough to skip re-scanning
#: on every message of the same conversation; `code_graph.index` is already
#: incremental, so this is purely to avoid one background thread per turn.
_STALE_AFTER_S = 300.0
_last_kicked: dict = {}


def _setting(key: str, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:  # noqa: BLE001
        return default


def maybe_auto_index(workspace: Optional[str]) -> bool:
    """Fire a background (re)index for `workspace` if it looks stale. Never
    raises, never blocks; returns True only when a background task was
    actually scheduled (mainly useful to tests)."""
    if not workspace or not bool(_setting("agent_code_graph_auto_index", True)):
        return False
    now = time.time()
    last = _last_kicked.get(workspace)
    if last is not None and now - last < _STALE_AFTER_S:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # not inside an async turn; nothing safe to schedule on

    def _run() -> None:
        try:
            from src import code_graph
            from src.context_engine import code_index
            status = code_index.status(workspace)
            if status.get("symbols"):
                age = None
                try:
                    from src.context_engine import store as ce_store
                    age = ce_store.age_seconds(status.get("last_indexed_at"))
                except Exception:  # noqa: BLE001
                    age = None
                if age is not None and age < _STALE_AFTER_S:
                    return
            code_graph.index(workspace)
        except Exception as exc:  # noqa: BLE001 - background convenience, never fatal
            logger.debug("code_graph auto-index skipped for %s: %s", workspace, exc)

    _last_kicked[workspace] = now
    try:
        loop.create_task(asyncio.to_thread(_run))
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_graph auto-index could not schedule for %s: %s", workspace, exc)
        return False
    return True
