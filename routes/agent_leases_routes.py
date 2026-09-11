"""Resource leases API — ``GET /api/agents/leases`` (PLAN-03).

Backend already refuses to let two delegated workers step on the same
resource (``src/subagent_permissions.py`` for permission rules,
``src/resource_ownership.py`` / ``FileLockRegistry`` for the leases
themselves; a worker that dies without releasing frees its leases once its
TTL lapses) — but nothing surfaced any of that to a screen. This is that
surface: the process-wide registry (``src.resource_ownership.get_registry``),
read-only, as one small JSON payload.

  GET /api/agents/leases → {
    "leases":    [{resource, kind, owner_agent, task_id, since, expires_at,
                    ttl_seconds, activity}, ...],
    "conflicts": [{resource, kind, holder_agent, holder_task_id,
                    requester_agent, requester_task_id, at}, ...],
  }

``leases`` is who holds what right now (oldest first). ``conflicts`` is the
record of a *refused* acquisition — a second task that asked for a resource
another one already held — so a screen can show "task B is waiting on a
resource task A owns" instead of only ever seeing one side of a collision;
see ``ResourceOwnershipRegistry.acquire()``'s docstring for exactly when one
of these is recorded. Neither list is scoped to the caller's own sessions:
the registry is process-wide by design (one place to see ownership across
every concurrent delegation run), so this is a read of shared state, gated
on being a logged-in caller like the rest of the agents surface — not a
per-session ownership check, because there is no single session that owns
a lease.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request

from src.auth_helpers import require_user
from src.resource_ownership import ConflictAttempt, Lease, get_registry


def _lease_row(lease: Lease) -> Dict[str, Any]:
    return {
        "resource": lease.resource,
        "kind": lease.kind,
        "owner_agent": lease.owner,
        "task_id": lease.task_id,
        "since": lease.acquired_at,
        "expires_at": lease.expires_at(),
        "ttl_seconds": lease.ttl_seconds,
        "activity": list(lease.activity),
    }


def _conflict_row(conflict: ConflictAttempt) -> Dict[str, Any]:
    return {
        "resource": conflict.resource,
        "kind": conflict.kind,
        "holder_agent": conflict.holder,
        "holder_task_id": conflict.holder_task_id,
        "requester_agent": conflict.requester,
        "requester_task_id": conflict.requester_task_id,
        "at": conflict.at,
    }


def setup_agent_leases_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agents", tags=["agents"])

    @router.get("/leases")
    def get_leases(request: Request) -> Dict[str, List[Dict[str, Any]]]:
        require_user(request)
        registry = get_registry()
        leases = sorted(registry.active_leases(), key=lambda lease: lease.acquired_at)
        conflicts = sorted(registry.conflicts(), key=lambda c: c.at)
        return {
            "leases": [_lease_row(lease) for lease in leases],
            "conflicts": [_conflict_row(c) for c in conflicts],
        }

    return router
