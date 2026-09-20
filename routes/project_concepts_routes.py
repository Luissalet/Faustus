"""routes/project_concepts_routes.py — HTTP surface for the agent's own
persistent, per-project architecture concept graph (src/project_concepts.py).

Same auth style as the neighbouring doc-claims/code-graph routes
(`routes/doc_claims_routes.py`, `routes/code_graph_routes.py`):
`require_admin` on every endpoint, and the workspace resolved through the
same active-workspace guard other admin tool routes use
(`src.code_graph.query._root`) when a `workspace` query param is given.
`project_id` is preferred when given and validated the way
`routes/requirements_routes.py` validates one (must belong to this owner).

    GET  /api/project-concepts                 ?project_id=|workspace=   -> roots
    GET  /api/project-concepts/graph           ?project_id=|workspace=   -> nodes+edges for the UI
    GET  /api/project-concepts/understand      ?project_id=|workspace=&q=&k=
    GET  /api/project-concepts/{id}            ?project_id=|workspace=
    GET  /api/project-concepts/{id}/history    ?project_id=|workspace=
    POST /api/project-concepts                 {project_id|workspace, name, kind, ...}
    POST /api/project-concepts/link            {project_id|workspace, src, dst, rel, note}
    DELETE /api/project-concepts/{id}          ?project_id=|workspace=
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import project_concepts as pc

logger = logging.getLogger(__name__)


class ConceptUpsertRequest(BaseModel):
    project_id: Optional[str] = None
    workspace: Optional[str] = None
    id: Optional[str] = Field(None, max_length=80)
    name: str = Field(..., min_length=1, max_length=300)
    kind: str = Field(..., min_length=1, max_length=20)
    summary: str = Field("", max_length=2000)
    details: str = Field("", max_length=200_000)
    refs: Optional[List[str]] = Field(None, max_length=64)
    parent_id: Optional[str] = Field(None, max_length=80)


class ConceptLinkRequest(BaseModel):
    project_id: Optional[str] = None
    workspace: Optional[str] = None
    src: str = Field(..., min_length=1, max_length=80)
    dst: str = Field(..., min_length=1, max_length=80)
    rel: str = Field(..., min_length=1, max_length=20)
    note: str = Field("", max_length=1000)


def _resolve_workspace(raw: str) -> str:
    if not raw:
        return ""
    try:
        from src.code_graph.query import _root
        return _root(raw)
    except Exception:  # noqa: BLE001
        return raw


def _store_for(project_id: str, workspace: str) -> pc.Store:
    ws = _resolve_workspace(workspace) if workspace else ""
    try:
        key = pc.resolve_project_key(project_id=project_id or None, workspace=ws or None)
    except pc.ProjectConceptsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return pc.Store(key)


def _pc_http_error(exc: pc.ProjectConceptsError) -> HTTPException:
    status = 404 if exc.error_class == "project_concepts.not_found" else 400
    return HTTPException(status, str(exc))


def setup_project_concepts_routes() -> APIRouter:
    router = APIRouter(prefix="/api/project-concepts", tags=["project-concepts"])

    @router.get("")
    def list_roots(request: Request, project_id: str = "", workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        return {"concepts": store.list_roots()}

    @router.get("/graph")
    def graph(request: Request, project_id: str = "", workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        return store.graph()

    @router.get("/understand")
    def understand(
        request: Request, q: str = "", k: int = pc.DEFAULT_UNDERSTAND_K,
        project_id: str = "", workspace: str = "",
    ) -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        if not q.strip():
            raise HTTPException(400, "q is required")
        return store.understand(q, k=k)

    @router.get("/{concept_id}")
    def get_concept(concept_id: str, request: Request, project_id: str = "", workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        item = store.get_concept(concept_id)
        if item is None:
            raise HTTPException(404, f"{concept_id} not found")
        return {"concept": item}

    @router.get("/{concept_id}/history")
    def concept_history(concept_id: str, request: Request, project_id: str = "", workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        return {"history": store.history(concept_id)}

    @router.get("/{concept_id}/stale")
    def concept_stale(concept_id: str, request: Request, project_id: str = "", workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        item = store.get_concept(concept_id)
        if item is None:
            raise HTTPException(404, f"{concept_id} not found")
        ws = _resolve_workspace(workspace) if workspace else ""
        return store.stale_check(item, ws)

    @router.post("", status_code=201)
    def upsert_concept(payload: ConceptUpsertRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(payload.project_id or "", payload.workspace or "")
        try:
            item = store.upsert_concept(
                name=payload.name, kind=payload.kind, summary=payload.summary,
                details=payload.details, refs=payload.refs, parent_id=payload.parent_id,
                concept_id=payload.id,
            )
        except pc.ProjectConceptsError as exc:
            raise _pc_http_error(exc) from exc
        return {"concept": item}

    @router.post("/link", status_code=201)
    def link_concepts(payload: ConceptLinkRequest, request: Request) -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(payload.project_id or "", payload.workspace or "")
        try:
            edge = store.link(payload.src, payload.dst, payload.rel, note=payload.note)
        except pc.ProjectConceptsError as exc:
            raise _pc_http_error(exc) from exc
        return {"edge": edge}

    @router.delete("/{concept_id}")
    def remove_concept(concept_id: str, request: Request, project_id: str = "", workspace: str = "") -> Dict[str, Any]:
        require_admin(request)
        store = _store_for(project_id, workspace)
        removed = store.remove_concept(concept_id)
        if not removed:
            raise HTTPException(404, f"{concept_id} not found")
        return {"removed": True, "id": concept_id}

    return router
