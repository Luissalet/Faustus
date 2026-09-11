"""routes/alternatives_routes.py — CMP-13: isolated, comparable alternatives
(``/api/projects/{project_id}/alternatives/*``).

Gating follows ``routes/git_routes.py``'s own split (its docstring is
explicit about the reasoning): reading is ``require_user``, every mutation
is ``require_human`` -- creating/removing an isolated worktree, running an
arbitrary test command inside one, and (especially) ``apply``/``combine``
writing into the user's main copy are all the same class of surprise
``require_human`` already exists to gate, not the user's own private data
(`board`'s ``require_user``-only class) and not a remote side effect either.

Errors follow ``_error()`` copied from ``routes/git_routes.py``/
``routes/board_routes.py`` verbatim: ``error_class`` at the top level next
to ``detail``, never nested, so a client that already renders one of those
error vocabularies renders this one the same way.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.middleware import require_human
from src.auth_helpers import effective_user, require_user
from src import alternatives
from services.projects import get_store as get_project_store

logger = logging.getLogger(__name__)


class CreateExperimentRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    workspace: Optional[str] = Field(None, max_length=4000, description="Absolute path; defaults to the project's own workspace")


class CreateDocExperimentRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000)
    base_content: str = Field("", max_length=2_000_000)
    document_id: Optional[str] = Field(
        None, max_length=200,
        description="W3-INT: a real Studio document this owner can read; when given, the "
                     "experiment's base text is that document's LIVE current_content (never "
                     "`base_content`) and apply/combine can write into its version history — "
                     "see docs/api/alternatives.md §Cableado a core.database.Document")


class AddAlternativeRequest(BaseModel):
    label: str = Field("", max_length=200)
    isolation: Optional[str] = Field(None, description="worktree | snapshot_dir | doc_version")


class DocContentRequest(BaseModel):
    content: str = Field("", max_length=2_000_000)


class RunTestsRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)
    timeout: Optional[float] = Field(None, ge=1, le=600)


class ApplyRequest(BaseModel):
    mine_doc_content: Optional[str] = Field(None, max_length=2_000_000)


class CombineRequest(BaseModel):
    choices: Dict[str, str] = Field(..., description="{relative_path: alternative_id}")
    mine_doc_content: Optional[str] = Field(None, max_length=2_000_000)


def setup_alternatives_routes() -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/alternatives", tags=["alternatives"])

    def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
        body: Dict[str, Any] = {"error_class": error_class}
        if detail is not None:
            body["detail"] = detail
        body.update(extra)
        return JSONResponse(status_code=status, content=body)

    def _alt_error(exc: alternatives.AlternativesError) -> JSONResponse:
        status = 404 if exc.error_class in ("alternatives.not_found", "alternatives.alt_not_found") else \
            409 if exc.error_class == "alternatives.apply_conflict" else \
            503 if exc.error_class == "dependency.missing" else 400
        extra: Dict[str, Any] = {}
        if isinstance(exc, alternatives.ApplyConflictError):
            extra["conflicts"] = exc.conflicts
        return _error(status, exc.error_class, str(exc), **extra)

    def _project_or_404(project_id: str, owner: Optional[str]) -> Dict[str, Any]:
        project = get_project_store().get(project_id, owner)
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    def _exp_or_error(owner: str, exp_id: str, project_id: str) -> Dict[str, Any]:
        exp = alternatives.get_experiment(owner, exp_id)
        if exp.get("project_id") != project_id:
            raise alternatives.ExperimentNotFoundError(f"no experiment {exp_id!r} in project {project_id!r}")
        return exp

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------
    @router.get("")
    def list_experiments(project_id: str, request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        return {"experiments": alternatives.list_experiments(owner, project_id)}

    @router.post("")
    def create_experiment(project_id: str, body: CreateExperimentRequest, request: Request,
                           _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        workspace = (body.workspace or "").strip() or str(project.get("workspace") or "").strip()
        if not workspace:
            return _error(400, "alternatives.invalid_request",
                           "no `workspace` given and this project has none configured")
        try:
            exp = alternatives.create_experiment(owner, project_id, body.goal, workspace)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)
        return exp

    @router.post("/doc")
    def create_doc_experiment(project_id: str, body: CreateDocExperimentRequest, request: Request,
                               _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            exp = alternatives.create_doc_experiment(
                owner, project_id, body.goal, body.base_content, document_id=body.document_id)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)
        return exp

    @router.get("/{exp_id}")
    def get_experiment(project_id: str, exp_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            return _exp_or_error(owner, exp_id, project_id)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    @router.delete("/{exp_id}")
    def delete_experiment(project_id: str, exp_id: str, request: Request,
                           _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            alternatives.delete_experiment(owner, exp_id)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)
        return {"ok": True}

    @router.get("/{exp_id}/compare")
    def compare(project_id: str, exp_id: str, request: Request, _u: str = Depends(require_user)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            return alternatives.compare(owner, exp_id)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    # ------------------------------------------------------------------
    # Alternatives
    # ------------------------------------------------------------------
    @router.post("/{exp_id}/alternatives")
    def add_alternative(project_id: str, exp_id: str, body: AddAlternativeRequest, request: Request,
                         _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            return alternatives.add_alternative(owner, exp_id, body.label, isolation=body.isolation)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    @router.put("/{exp_id}/alternatives/{alt_id}/content")
    def set_doc_content(project_id: str, exp_id: str, alt_id: str, body: DocContentRequest, request: Request,
                         _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            return alternatives.set_doc_version_content(owner, exp_id, alt_id, body.content)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    @router.post("/{exp_id}/alternatives/{alt_id}/tests")
    def run_tests(project_id: str, exp_id: str, alt_id: str, body: RunTestsRequest, request: Request,
                  _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            kwargs = {"timeout": body.timeout} if body.timeout else {}
            return alternatives.run_tests(owner, exp_id, alt_id, body.command, **kwargs)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    @router.post("/{exp_id}/apply/{alt_id}")
    def apply_alternative(project_id: str, exp_id: str, alt_id: str, body: ApplyRequest, request: Request,
                           _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            return alternatives.apply_alternative(owner, exp_id, alt_id, mine_doc_content=body.mine_doc_content)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    @router.post("/{exp_id}/combine")
    def combine(project_id: str, exp_id: str, body: CombineRequest, request: Request,
                _h: None = Depends(require_human)) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _exp_or_error(owner, exp_id, project_id)
            return alternatives.combine(owner, exp_id, body.choices, mine_doc_content=body.mine_doc_content)
        except alternatives.AlternativesError as exc:
            return _alt_error(exc)

    return router
