"""Project work board API (Lote 92 / OBJ-6) — ``/api/projects/{project_id}/board/*``.

A per-project issue tracker (BOARD_RESEARCH.md, CONTRATO_BOARD): readable
ids (``FAU-12``), a fixed vocabulary of type/status/priority, simple graph
links, and ``GET .../summary`` — the compact projection that
``src.agent_loop._project_board_block`` injects into the system prompt
instead of the agent re-reading a markdown backlog in full every turn.

Gating follows ``docs/api/git.md``'s own contract note (CONTRATO_BOARD is
explicit about this too): owner-scoped throughout via ``require_user``, and
every mutation ALSO stays ``require_user`` rather than ``require_human`` —
unlike the git panel, a board issue is the user's own data with no external
side effect (no remote host, no other person notified), so it belongs in the
same gate class as ``manage_documents``/``manage_notes``, not the git write
routes. ``routes/project_routes.py``'s ``_get_or_404`` shape is copied
verbatim for ownership: a project id that is not this owner's answers exactly
like one that does not exist.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from src.auth_helpers import effective_user, require_user
from src import project_board
from services.projects import ProjectError, get_store as get_project_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
class IssueLinkRef(BaseModel):
    kind: str = Field(..., min_length=1, max_length=32)
    target: str = Field(..., min_length=1, max_length=40)


class IssueCreateRequest(BaseModel):
    type: str = Field(..., min_length=1, max_length=20)
    title: str = Field(..., min_length=1, max_length=300)
    body_md: str = Field("", max_length=200_000)
    priority: Optional[str] = Field(None, max_length=4)
    assignee: Optional[str] = Field(None, max_length=120)
    labels: Optional[List[str]] = Field(None, max_length=40)
    links: Optional[List[IssueLinkRef]] = Field(None, max_length=40)


class IssueUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    body_md: Optional[str] = Field(None, max_length=200_000)
    type: Optional[str] = Field(None, max_length=20)
    status: Optional[str] = Field(None, max_length=20)
    priority: Optional[str] = Field(None, max_length=4)
    assignee: Optional[str] = Field(None, max_length=120)
    labels: Optional[List[str]] = Field(None, max_length=40)


class ClaimRequest(BaseModel):
    assignee: str = Field(..., min_length=1, max_length=120)


class CommentRequest(BaseModel):
    body_md: str = Field(..., min_length=1, max_length=200_000)


class LinkRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=32)
    target: str = Field(..., min_length=1, max_length=40)


class RefRequest(BaseModel):
    kind: str = Field(..., min_length=1, max_length=20)
    value: str = Field(..., min_length=1, max_length=2000)
    label: str = Field("", max_length=300)


class ImportRequest(BaseModel):
    sources: List[str] = Field(default_factory=lambda: ["objetivos", "pendientes", "backlog"])
    dry_run: bool = True


class KeyRequest(BaseModel):
    key: str = Field(..., min_length=2, max_length=5)


def setup_board_routes() -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}/board", tags=["board"])

    def _project_or_404(project_id: str, owner: Optional[str]) -> Dict[str, Any]:
        project = get_project_store().get(project_id, owner)
        if not project:
            raise HTTPException(404, "Project not found")
        return project

    def _key_for(project: Dict[str, Any]) -> str:
        return get_project_store().board_key(project)

    def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
        """A flat error body -- `error_class` sits next to `detail` at the TOP
        level, not nested under it the way plain `HTTPException(status, {...})`
        would (FastAPI wraps a dict `detail` as `{"detail": {...}}`, and
        `core.middleware`'s OBS-03 middleware only backfills a GENERIC
        status-derived `error_class` when the body doesn't already have one --
        it would never see the specific `board.not_found` /
        `board.invalid_transition` / `board.claimed` CONTRATO_BOARD promises,
        because that class would be buried in `detail.code` instead). Copied
        verbatim from `routes/git_routes.py`'s own `_error()` -- the pattern
        rule 7 (BRIEF_CIERRE) says to follow."""
        body: Dict[str, Any] = {"error_class": error_class}
        if detail is not None:
            body["detail"] = detail
        body.update(extra)
        return JSONResponse(status_code=status, content=body)

    def _board_error(exc: project_board.BoardError) -> JSONResponse:
        status = 409 if exc.error_class == "board.claimed" else \
            404 if exc.error_class == "board.not_found" else 400
        extra: Dict[str, Any] = {}
        holder = getattr(exc, "holder", None)
        if holder:
            extra["holder"] = holder
        return _error(status, exc.error_class, str(exc), **extra)

    def _issue_or_404(issue_id: str, project_id: str) -> Dict[str, Any]:
        """Raises `project_board.NotFoundError` (a `BoardError`) rather than
        an `HTTPException` directly, so every caller's own
        `except project_board.BoardError` -> `_board_error()` already handles
        it uniformly, with the real `board.not_found` class at the top level
        (see `_error()` above) instead of a generic one."""
        issue = project_board.get(issue_id)
        if not issue or issue.get("project_id") != project_id:
            raise project_board.NotFoundError("Issue not found")
        return issue

    # ------------------------------------------------------------------
    # Listing / retrieval
    # ------------------------------------------------------------------
    @router.get("/issues")
    def list_issues(
        project_id: str, request: Request, status: str = "", type: str = "",
        assignee: str = "", q: str = "", priority: str = "", label: str = "",
        limit: int = 50, cursor: str = "", _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        issues, next_cursor = project_board.list_issues(
            project_id, status=status, type=type, assignee=assignee, q=q,
            priority=priority, label=label, limit=limit, cursor=cursor or None,
        )
        return {"issues": issues, "next_cursor": next_cursor}

    @router.get("/ready")
    def ready(project_id: str, request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        return {"issues": project_board.ready_issues(project_id)}

    @router.get("/summary")
    def summary(project_id: str, request: Request, _u: str = Depends(require_user)) -> Dict[str, Any]:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        data = project_board.summary(project_id)
        data["key"] = _key_for(project)
        return data

    @router.get("/export.md")
    def export_md(project_id: str, request: Request, _u: str = Depends(require_user)) -> PlainTextResponse:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        text = project_board.export_markdown(project_id, _key_for(project), project.get("name") or "")
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    @router.post("/issues", status_code=201)
    def create_issue(
        project_id: str, payload: IssueCreateRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        try:
            issue = project_board.create_issue(
                project_id, _key_for(project), type=payload.type, title=payload.title,
                body_md=payload.body_md, priority=payload.priority or project_board.DEFAULT_PRIORITY,
                assignee=payload.assignee or "", labels=payload.labels or [],
                created_by=owner or "user",
                links=[l.model_dump() for l in (payload.links or [])],
            )
        except project_board.BoardError as e:
            return _board_error(e)
        return {"issue": issue}

    @router.get("/issues/{issue_id}")
    def get_issue(
        project_id: str, issue_id: str, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            issue = _issue_or_404(issue_id, project_id)
        except project_board.BoardError as e:
            return _board_error(e)
        return {"issue": issue}

    @router.patch("/issues/{issue_id}")
    def update_issue(
        project_id: str, issue_id: str, payload: IssueUpdateRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        # `exclude_unset` (not `exclude_none`): a client that explicitly sends
        # `"assignee": null` to CLEAR the assignee must reach
        # `project_board.update_issue` with that key present -- `exclude_none`
        # would silently drop it (indistinguishable from never having sent
        # it), making "unassign" impossible through this route even though
        # the store fully supports a falsy `assignee` in the patch dict.
        patch = payload.model_dump(exclude_unset=True)
        if not patch:
            return _error(400, "board.invalid", "Nothing to update")
        try:
            _issue_or_404(issue_id, project_id)
            issue = project_board.update_issue(issue_id, patch, actor=owner or "user")
        except project_board.BoardError as e:
            return _board_error(e)
        return {"issue": issue}

    @router.post("/issues/{issue_id}/claim")
    def claim_issue(
        project_id: str, issue_id: str, payload: ClaimRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _issue_or_404(issue_id, project_id)
            issue = project_board.claim(issue_id, payload.assignee, actor=owner or "user")
        except project_board.BoardError as e:
            return _board_error(e)
        return {"issue": issue}

    @router.post("/issues/{issue_id}/comments", status_code=201)
    def add_comment(
        project_id: str, issue_id: str, payload: CommentRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _issue_or_404(issue_id, project_id)
            comment = project_board.add_comment(issue_id, payload.body_md, author=owner or "user")
        except project_board.BoardError as e:
            return _board_error(e)
        return {"comment": comment}

    @router.post("/issues/{issue_id}/links", status_code=201)
    def add_link(
        project_id: str, issue_id: str, payload: LinkRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _issue_or_404(issue_id, project_id)
            link = project_board.add_link(issue_id, payload.kind, payload.target, actor=owner or "user")
        except project_board.BoardError as e:
            return _board_error(e)
        return {"link": link}

    @router.delete("/issues/{issue_id}/links/{link_id}")
    def remove_link(
        project_id: str, issue_id: str, link_id: str, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _issue_or_404(issue_id, project_id)
        except project_board.BoardError as e:
            return _board_error(e)
        if not project_board.remove_link(issue_id, link_id):
            return _error(404, "board.not_found", "Link not found")
        return {"success": True}

    @router.post("/issues/{issue_id}/refs", status_code=201)
    def add_ref(
        project_id: str, issue_id: str, payload: RefRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _issue_or_404(issue_id, project_id)
            ref = project_board.add_ref(issue_id, payload.kind, payload.value, label=payload.label)
        except project_board.BoardError as e:
            return _board_error(e)
        return {"ref": ref}

    @router.delete("/issues/{issue_id}")
    def delete_issue(
        project_id: str, issue_id: str, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        """Only the owner (every route in this file already requires that) —
        deletes the issue and its comments/events/links/refs in cascade."""
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            _issue_or_404(issue_id, project_id)
        except project_board.BoardError as e:
            return _board_error(e)
        project_board.delete_issue(issue_id)
        return {"success": True}

    # ------------------------------------------------------------------
    # Import / key
    # ------------------------------------------------------------------
    @router.post("/import")
    def import_sources(
        project_id: str, payload: ImportRequest, request: Request,
        _u: str = Depends(require_user),
    ) -> Dict[str, Any]:
        owner = effective_user(request)
        project = _project_or_404(project_id, owner)
        allowed = {"objetivos", "pendientes", "backlog"}
        sources = [s for s in payload.sources if s in allowed]
        result = project_board.import_sources(
            project, _key_for(project), sources=sources, dry_run=payload.dry_run,
        )
        return result

    @router.put("/key")
    def set_key(
        project_id: str, payload: KeyRequest, request: Request, _u: str = Depends(require_user),
    ) -> Any:
        owner = effective_user(request)
        _project_or_404(project_id, owner)
        try:
            updated = get_project_store().set_board_key(project_id, payload.key, owner)
        except ProjectError as e:
            return _error(400, "board.invalid_key", str(e))
        if not updated:
            raise HTTPException(404, "Project not found")
        return {"key": get_project_store().board_key(updated)}

    return router
