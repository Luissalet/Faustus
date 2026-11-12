"""Harness evolution (dictamen §7): propose/validate/evaluate/promote/
rollback a `CandidatePatch` against the running agent harness's own
versioned revisions.

`require_admin` on every endpoint — this changes what the harness itself
runs for everyone, not a single owner's own data, the same reasoning
`tool_registry_routes.py` gives for its own admin gate.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin, require_human
from src.harness_evolution import cycles, promotion, service as evolution_service
from src.harness_evolution.store import StaleParent


class ProposeCandidateBody(BaseModel):
    changes: dict
    source_trace_ids: list[str] = []
    scope: str = ""
    required_capabilities: dict[str, str] = {}
    parent_revision: str | None = None


class PromoteCandidateBody(BaseModel):
    canary_runs: int | None = None


class RollbackCandidateBody(BaseModel):
    reason: str = "manual rollback"


class GaltonCasesBody(BaseModel):
    url: str
    suites: list[str]


def _service() -> evolution_service.HarnessEvolutionService:
    # A fresh store handle per call — `store.py`'s own connections are
    # already per-call; this just avoids holding a service singleton with
    # stale settings across the process lifetime.
    return evolution_service.HarnessEvolutionService()


def setup_evolution_routes() -> APIRouter:
    router = APIRouter(prefix="/api/harness", tags=["harness-evolution"])

    @router.get("/revisions")
    def list_revisions(request: Request):
        require_admin(request)
        svc = _service()
        return {"ok": True, "revisions": [r.to_dict() for r in svc.list_revisions()],
               "active_revision_id": svc.active_revision().revision_id}

    @router.get("/candidates")
    def list_candidates(request: Request):
        require_admin(request)
        svc = _service()
        return {"ok": True, "candidates": [p.to_dict() for p in svc.list_patches()]}

    @router.get("/candidates/{patch_id}")
    def get_candidate(request: Request, patch_id: str):
        require_admin(request)
        svc = _service()
        patch = svc.get_patch(patch_id)
        if patch is None:
            raise HTTPException(404, "no such candidate")
        return {"ok": True, "candidate": patch.to_dict()}

    @router.post("/candidates")
    def propose_candidate(request: Request, body: ProposeCandidateBody):
        require_admin(request)
        svc = _service()
        try:
            patch = svc.propose_candidate(
                changes=body.changes, source_trace_ids=body.source_trace_ids,
                scope=body.scope, required_capabilities=body.required_capabilities,
                parent_revision=body.parent_revision)
        except evolution_service.HarnessEvolutionError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": patch.status != "rejected", "candidate": patch.to_dict()}

    @router.post("/candidates/{patch_id}/promote")
    def promote_candidate(request: Request, patch_id: str, body: PromoteCandidateBody):
        require_admin(request)
        svc = _service()
        actor = str(getattr(request.state, "current_user", "") or "admin")
        try:
            revision = svc.promote_candidate(patch_id, actor=actor,
                                             canary_runs=body.canary_runs)
        except StaleParent as exc:
            raise HTTPException(409, {"error": "stale_parent",
                                      "current_revision": exc.current.to_dict()})
        except (evolution_service.HarnessEvolutionError, promotion.PromotionRefused,
                promotion.CanaryFailed) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "revision": revision.to_dict()}

    @router.post("/candidates/{patch_id}/rollback")
    def rollback_candidate(request: Request, patch_id: str, body: RollbackCandidateBody):
        require_admin(request)
        svc = _service()
        actor = str(getattr(request.state, "current_user", "") or "admin")
        try:
            revision = svc.rollback_candidate(patch_id, actor=actor, reason=body.reason)
        except evolution_service.HarnessEvolutionError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "revision": revision.to_dict()}

    from src.auth_helpers import get_current_user
    import asyncio

    def cycle_owner(request):
        from src.auth_helpers import effective_user
        from src.owner_identity import effective_storage_owner, is_request_sentinel_owner
        owner = effective_storage_owner(effective_user(request))
        if not owner or is_request_sentinel_owner(owner):
            raise HTTPException(400, "El ciclo necesita un propietario concreto.")
        return owner

    @router.get("/cycles/cases")
    def cycle_cases(request: Request):
        cycle_owner(request)
        return {"cases": [c.model_dump() for c in cycles.daily_cases()]}

    @router.post("/cycles/galton/cases")
    async def import_galton_cases(request: Request, body: GaltonCasesBody):
        cycle_owner(request)
        if not 1 <= len(body.suites) <= 20: raise HTTPException(400, "Selecciona entre 1 y 20 suites.")
        try:
            return {"cases": await cycles.galton_cases(body.url, body.suites)}
        except Exception as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/cycles")
    def list_cycles(request: Request):
        owner = cycle_owner(request)
        return {"cycles": cycles.CycleStore().list(owner)}

    @router.get("/cycles/{cycle_id}")
    def get_cycle(request: Request, cycle_id: str):
        owner = cycle_owner(request)
        record = cycles.CycleStore().get(cycle_id, owner)
        if not record: raise HTTPException(404, "No existe el ciclo.")
        patches = []
        svc = _service()
        for item in record["candidates"]:
            patch = svc.get_patch(item.get("patch_id", ""))
            if patch: patches.append(patch.to_dict())
        return {"cycle": record, "patches": patches}

    @router.post("/cycles")
    async def start_cycle(request: Request, body: cycles.CycleSpec):
        owner = cycle_owner(request)
        record = cycles.create(body, owner)
        task = asyncio.create_task(cycles.run(record["id"], owner))
        _CYCLE_TASKS.add(task)
        task.add_done_callback(_CYCLE_TASKS.discard)
        return {"cycle": record}

    @router.post("/cycles/patches/{patch_id}/revert")
    def revert_cycle_patch(request: Request, patch_id: str):
        try:
            return {"revision": cycles.revert(patch_id, cycle_owner(request))}
        except (ValueError, evolution_service.HarnessEvolutionError, StaleParent) as exc:
            raise HTTPException(409, str(exc)) from None

    @router.post("/cycles/patches/{patch_id}/accept")
    def accept_cycle_patch(request: Request, patch_id: str):
        require_human(request)
        try:
            return {"revision": cycles.accept(patch_id, cycle_owner(request))}
        except (ValueError, evolution_service.HarnessEvolutionError, StaleParent, promotion.PromotionRefused) as exc:
            raise HTTPException(409, str(exc)) from None

    return router


_CYCLE_TASKS: set = set()
