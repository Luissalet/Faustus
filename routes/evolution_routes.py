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

from core.middleware import require_admin
from src.harness_evolution import promotion, service as evolution_service
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

    return router
