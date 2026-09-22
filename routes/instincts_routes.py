"""routes/instincts_routes.py — REST API for Lot I's learned instincts.

Owner-scoped like `routes/skills_routes.py` (same `get_current_user` helper,
same "your own data only" posture) — NOT admin-only. See `src/instincts.py`
for the actual store/behaviour; this module is a thin HTTP adapter over it.

    GET    /api/instincts?project=&min_confidence=
    GET    /api/instincts/status?project=
    GET    /api/instincts/export
    GET    /api/instincts/{id}
    POST   /api/instincts                          (add, manual)
    POST   /api/instincts/promote                  {id?, dry_run?}
    POST   /api/instincts/evolve                   {project?, generate?}
    POST   /api/instincts/import                   {json}
    POST   /api/instincts/extract                  {session_id}
    POST   /api/instincts/{id}/confirm
    POST   /api/instincts/{id}/contradict
    POST   /api/instincts/{id}/retire
    DELETE /api/instincts/{id}

Literal sub-paths (`status`, `export`, `promote`, `evolve`, `import`,
`extract`) are registered before the single-segment `/{id}` routes so
FastAPI's registration-order matching never lets `{id}` swallow them.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src import instincts
from src.auth_helpers import get_current_user

logger = logging.getLogger(__name__)


class AddInstinctRequest(BaseModel):
    trigger: str = Field(..., min_length=1, max_length=200)
    action: str = Field(..., min_length=1, max_length=300)
    domain: str = "other"
    scope: str = "project"
    project: str = ""
    project_name: str = ""


class EvidenceRequest(BaseModel):
    evidence: Optional[Dict[str, Any]] = None


class PromoteRequest(BaseModel):
    id: Optional[str] = None
    dry_run: bool = False


class EvolveRequest(BaseModel):
    project: Optional[str] = None
    generate: bool = False
    min_cluster: int = 2


class ImportRequest(BaseModel):
    # Field is named `payload` (not `json`) to avoid shadowing BaseModel's own
    # deprecated `.json()` method; the wire field stays "json" via the alias
    # so the request body shape matches the module docstring's
    # `POST /api/instincts/import {json}`.
    payload: str = Field(..., min_length=1, alias="json")
    scope_override: Optional[str] = None
    model_config = {"populate_by_name": True}


class ExtractRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


def setup_instincts_routes() -> APIRouter:
    router = APIRouter(prefix="/api/instincts", tags=["instincts"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    # ── literal sub-paths first (see module docstring) ──────────────────

    @router.get("/status")
    async def get_status(request: Request, project: Optional[str] = None):
        return instincts.status(_owner(request), project=project)

    @router.get("/export")
    async def get_export(request: Request):
        return {"json": instincts.export_json(_owner(request))}

    @router.post("/promote")
    async def post_promote(request: Request, body: PromoteRequest):
        return instincts.promote(_owner(request), id=body.id, dry_run=body.dry_run)

    @router.post("/evolve")
    async def post_evolve(request: Request, body: EvolveRequest):
        return instincts.evolve(
            _owner(request), project=body.project,
            min_cluster=body.min_cluster, generate=body.generate,
        )

    @router.post("/import")
    async def post_import(request: Request, body: ImportRequest):
        try:
            return instincts.import_json(_owner(request), body.payload, scope_override=body.scope_override)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @router.post("/extract")
    async def post_extract(request: Request, body: ExtractRequest):
        """Manual trigger: run extraction on one session's own messages."""
        owner = _owner(request)
        from core.database import ChatMessage, SessionLocal
        from services.projects import project_context_for_session

        db = SessionLocal()
        try:
            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_id == body.session_id)
                .order_by(ChatMessage.timestamp.asc())
                .all()
            )
        finally:
            db.close()
        if not rows:
            raise HTTPException(404, f"session {body.session_id!r} has no messages")
        messages: List[Dict[str, str]] = [{"role": r.role, "content": r.content or ""} for r in rows]

        ctx = project_context_for_session(body.session_id, owner)
        stored = await instincts.extract_from_turn(
            owner, body.session_id, messages,
            project=instincts.project_key(ctx.workspace, ctx.project_id),
            project_name=ctx.project_name, workspace=ctx.workspace,
        )
        return {"stored": stored, "count": len(stored)}

    # ── collection ────────────────────────────────────────────────────────

    @router.get("")
    async def list_instincts_route(
        request: Request, project: Optional[str] = None,
        min_confidence: float = 0.0, include_global: bool = True,
    ):
        items = instincts.list_instincts(
            _owner(request), project=project,
            include_global=include_global, min_confidence=min_confidence,
        )
        return {"instincts": items, "count": len(items)}

    @router.post("")
    async def add_instinct(request: Request, body: AddInstinctRequest):
        record = instincts.add(
            _owner(request), trigger=body.trigger, action=body.action,
            domain=body.domain, scope=body.scope,
            project=body.project, project_name=body.project_name,
        )
        return record

    # ── single instinct ───────────────────────────────────────────────────

    @router.get("/{id}")
    async def get_instinct(request: Request, id: str):
        item = instincts.get(_owner(request), id)
        if item is None:
            raise HTTPException(404, "Instinct not found")
        return item

    @router.delete("/{id}")
    async def delete_instinct(request: Request, id: str):
        if not instincts.delete(_owner(request), id):
            raise HTTPException(404, "Instinct not found")
        return {"deleted": True, "id": id}

    @router.post("/{id}/confirm")
    async def confirm_instinct(request: Request, id: str, body: EvidenceRequest):
        try:
            return instincts.confirm(_owner(request), id, evidence=body.evidence)
        except KeyError:
            raise HTTPException(404, "Instinct not found")

    @router.post("/{id}/contradict")
    async def contradict_instinct(request: Request, id: str, body: EvidenceRequest):
        try:
            return instincts.contradict(_owner(request), id, evidence=body.evidence)
        except KeyError:
            raise HTTPException(404, "Instinct not found")

    @router.post("/{id}/retire")
    async def retire_instinct(request: Request, id: str):
        try:
            return instincts.retire(_owner(request), id)
        except KeyError:
            raise HTTPException(404, "Instinct not found")

    return router
