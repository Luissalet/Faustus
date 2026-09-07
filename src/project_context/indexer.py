"""Idle worker for queued project-context indexes.

Links are truth in ``projects.json``; the SQLite index is disposable derived
data.  This worker never guesses an owner, yields while a user turn is active,
and lets :class:`ProjectContextService` perform both authorisation checks and
the atomic publication.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .models import ActorRef
from .service import service

logger = logging.getLogger(__name__)

PENDING_STATUSES = frozenset({"queued", "stale", "indexing"})


def should_yield() -> bool:
    try:
        from src.agent_runs import active_session_ids
        return bool(active_session_ids())
    except Exception:  # noqa: BLE001 - a failed probe must not kill upkeep
        logger.debug("project indexer could not probe interactive work", exc_info=True)
        return False


def run_pending(projects: Sequence[Mapping[str, Any]] = (), *,
                budget_s: float = 20.0, max_links: int = 16) -> List[Dict[str, Any]]:
    """Index pending links from explicit owner-scoped project objects."""
    if should_yield():
        return [{"ok": True, "status": "yielded", "reason": "interactive_run"}]
    try:
        ceiling = max(1.0, float(budget_s))
    except (TypeError, ValueError):
        ceiling = 20.0
    cap = max(1, min(int(max_links or 16), 200))
    began = time.monotonic()
    rows: List[Dict[str, Any]] = []
    seen = set()
    worker = service()
    actor = ActorRef(kind="system", name="project-context-indexer")

    for raw in projects or ():
        if len(rows) >= cap or time.monotonic() - began >= ceiling:
            return rows
        project = dict(raw or {})
        project_id = str(project.get("id") or "").strip()
        owner = str(project.get("owner") or "").strip()
        scope = (owner, project_id)
        if not owner or not project_id or scope in seen:
            continue
        seen.add(scope)
        try:
            links = worker.list(project=project, owner=owner)
        except Exception as exc:  # noqa: BLE001 - isolate a project's storage failure
            logger.exception("project context indexer could not list %s", project_id)
            rows.append({"ok": False, "project_id": project_id, "status": "failed",
                         "error": f"{type(exc).__name__}: {exc}"})
            continue
        for link in links:
            if (not link.enabled or link.retrieval_policy == "disabled"
                    or link.index_status not in PENDING_STATUSES):
                continue
            if len(rows) >= cap or time.monotonic() - began >= ceiling:
                return rows
            if should_yield():
                rows.append({"ok": True, "status": "yielded",
                             "reason": "interactive_run"})
                return rows
            try:
                rows.append(worker.index(project=project, owner=owner,
                                         link_id=link.id, actor=actor).to_dict())
            except Exception as exc:  # noqa: BLE001 - continue with other projects
                logger.exception("project context indexer failed for %s/%s",
                                 project_id, link.id)
                rows.append({"ok": False, "project_id": project_id,
                             "link_id": link.id, "status": "failed",
                             "error": f"{type(exc).__name__}: {exc}"})
    return rows


async def scheduler_loop(*, projects_provider: Optional[
        Callable[[], Sequence[Mapping[str, Any]]]] = None,
        interval_s: Optional[float] = None, budget_s: float = 20.0) -> None:
    """Consume pending indexes until app shutdown."""
    if interval_s is None:
        try:
            from src.settings import get_setting
            interval_s = float(get_setting(
                "agent_project_context_index_seconds", 15) or 15)
        except Exception:  # noqa: BLE001
            interval_s = 15.0
    interval = max(5.0, min(float(interval_s), 3600.0))
    await asyncio.sleep(min(15.0, interval))
    while True:
        try:
            if projects_provider is None:
                from services.projects import get_store
                projects = await asyncio.to_thread(get_store().list, None)
            else:
                projects = await asyncio.to_thread(projects_provider)
            await asyncio.to_thread(run_pending, projects, budget_s=budget_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduled project context indexing failed: %s", exc)
        await asyncio.sleep(interval)


__all__ = ["PENDING_STATUSES", "should_yield", "run_pending", "scheduler_loop"]
