"""Versioned requirements API (ADP-18/19/20) — ``/api/projects/{project_id}/requirements/*``.

A per-project spec: readable ids (``REQ-3``, sequential per project -- see
``docs/requirements-format.md``), immutable revisions on every change, links
to code/tests/runs/issues with an independently-computed
linked/implemented/tested/verified/stale coverage matrix
(``src/requirements/evidence.py``), and a budgeted "what does THIS task need"
projection (``src/requirements/context.py``) so the agent does not re-read
the whole spec every turn -- the same shape ``routes/board_routes.py``'s
``GET .../summary`` already gives the project's issue list.

Gating: owner-scoped throughout via ``require_user`` and
``_project_or_404`` (verbatim copy of ``board_routes.py``'s own helper --
a project id that is not this owner's answers exactly like one that does
not exist, never leaking whether the id exists at all). Every write also
stays ``require_user`` rather than ``require_human``: a requirement is the
project's own spec data with no external side effect, the same class the
board's issues sit in.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.auth_helpers import effective_user, require_user
from src import requirements as req
from services.projects import get_store as get_project_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class RequirementCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    text: str = Field("", max_length=200_000)
    source: str = Field("human", max_length=10)
    acceptance: Optional[List[str]] = Field(None, max_length=100)
    proposed_by: str = Field("human", max_length=10)
    status: Optional[str] = Field(None, max_length=20)


class RequirementUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    text: Optional[str] = Field(None, max_length=200_000)
    source: Optional[str] = Field(None, max_length=10)
    acceptance: Optional[List[str]] = Field(None, max_length=100)
    status: Optional[str] = Field(None, max_length=20)
    by: str = Field("human", max_length=10)
    change_note: str = Field("", max_length=1000)


class LinkCreateRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=20)
    target: str = Field(..., min_length=1, max_length=2000)
    revision: str = Field("", max_length=100)


class ContextRequest(BaseModel):
    files: Optional[List[str]] = Field(None, max_length=100)
    keys: Optional[List[str]] = Field(None, max_length=100)
    budget_chars: int = Field(req.DEFAULT_BUDGET_CHARS, ge=0, le=1_000_000)


def setup_requirements_routes() -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/requirements", tags=["requirements"])

    def _project_or_404(project_id: str, owner: Optional[str]) -> Dict[str, Any]:
        project = get_project_store().get(project_id, owner)
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
        """Flat error body -- `error_class` at the top level, same shape
        `routes/git_routes.py::_error` and `routes/board_routes.py::_error`
        already use (copied verbatim, per CONTRATO_ADP_W1 rule 7)."""
        body: Dict[str, Any] = {"error_class": error_class}
        if detail is not None:
            body["detail"] = detail
        body.update(extra)
        return JSONResponse(status_code=status, content=body)

    def _req_error(exc: req.RequirementsError) -> JSONResponse:
        status = 404 if exc.error_class == "requirements.not_found" else \
            409 if exc.error_class == "requirements.path_outside_workspace" else 400
        return _error(status, exc.error_class, str(exc))

    # ------------------------------------------------------------------
    # Listing / retrieval
    # ------------------------------------------------------------------
    @router.get("")
    def list_requirements(
        project_id: str, request: Request, status: str = "", source: str = "", q: str = "",
        _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        items = req.list_requirements(project_id, status=status, source=source, q=q)
        return {"requirements": items}

    # NOTE: `/matrix` and `/sidecar` (single path segment, GET) MUST be
    # declared before `GET /{key}` -- FastAPI/Starlette match GET routes in
    # declaration order, so a `/{key}` registered first would swallow
    # `GET .../requirements/matrix` as key="matrix" and it would never reach
    # the route below.
    @router.get("/matrix")
    def project_matrix(
        project_id: str, request: Request, _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        rows = req.project_matrix(project_id, workspace=project.get("workspace") or "")
        return {"matrix": rows}

    @router.get("/sidecar")
    def sidecar_preview(
        project_id: str, request: Request, _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        return req.read_sidecar(project.get("workspace") or "")

    @router.get("/{key}")
    def get_requirement(
        project_id: str, key: str, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        item = req.get(project_id, key)
        if item is None:
            return _error(404, "requirements.not_found", f"{key} not found")
        return {"requirement": item}

    @router.get("/{key}/revisions")
    def get_revisions(
        project_id: str, key: str, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        if req.get(project_id, key) is None:
            return _error(404, "requirements.not_found", f"{key} not found")
        return {"revisions": req.revisions(project_id, key)}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    @router.post("", status_code=201)
    def create_requirement(
        project_id: str, payload: RequirementCreateRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            item = req.create(
                project_id, title=payload.title, text=payload.text, source=payload.source,
                acceptance=payload.acceptance or [], proposed_by=payload.proposed_by,
                status=payload.status, created_by=owner or "user",
            )
        except req.RequirementsError as e:
            return _req_error(e)
        return {"requirement": item}

    @router.patch("/{key}")
    def update_requirement(
        project_id: str, key: str, payload: RequirementUpdateRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        patch = payload.model_dump(exclude_unset=True, exclude={"by", "change_note"})
        if not patch:
            return _error(400, "requirements.invalid", "Nothing to update")
        try:
            item = req.update(
                project_id, key, patch, by=payload.by, actor=owner or "user",
                change_note=payload.change_note,
            )
        except req.RequirementsError as e:
            return _req_error(e)
        return {"requirement": item}

    # ------------------------------------------------------------------
    # Links / matrix
    # ------------------------------------------------------------------
    @router.get("/{key}/links")
    def list_links(
        project_id: str, key: str, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        if req.get(project_id, key) is None:
            return _error(404, "requirements.not_found", f"{key} not found")
        return {"links": req.list_links(project_id, key)}

    @router.post("/{key}/links", status_code=201)
    def add_link(
        project_id: str, key: str, payload: LinkCreateRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        try:
            link = req.link_evidence(
                project_id, key, kind=payload.kind, target=payload.target,
                workspace=project.get("workspace") or "", revision=payload.revision,
                created_by=owner or "user",
            )
        except req.RequirementsError as e:
            return _req_error(e)
        return {"link": link}

    @router.get("/{key}/matrix")
    def requirement_matrix(
        project_id: str, key: str, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        try:
            row = req.matrix(project_id, key, workspace=project.get("workspace") or "")
        except req.RequirementsError as e:
            return _req_error(e)
        return {"matrix": row}

    # ------------------------------------------------------------------
    # Context for a task (ADP-19)
    # ------------------------------------------------------------------
    @router.post("/context")
    def context_for_task(
        project_id: str, payload: ContextRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        return req.for_task(
            project_id, files=payload.files or [], keys=payload.keys or [],
            budget_chars=payload.budget_chars,
        )

    return router
