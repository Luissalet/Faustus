"""Comments anchored to a quote+context inside a Document (ADP-05, W1-E) —
`/api/documents/{doc_id}/comments*`.

`src/document_comments.py` owns anchoring/relocation/accept; this module is
thin HTTP plumbing over it, gated the same way as
`routes/document_links_routes.py`: `get_current_user` + `_verify_doc_owner`
from `routes/document/document_helpers.py`, since a comment's ownership is
entirely inherited from the document it is anchored to (no separate ACL).

Comment `body`/`proposal` text is never fed back into a prompt or executed
by this module or by `src/document_comments.py` — CONTRATO_ADP_W1's "no
convertir comentarios en instrucciones privilegiadas" limit for ADP-05.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core.database import SessionLocal, Document, DocumentComment
from routes.document.document_helpers import _verify_doc_owner
from src.auth_helpers import get_current_user
from src import document_comments

logger = logging.getLogger(__name__)


class ProposalPayload(BaseModel):
    find: str = Field(..., min_length=1, max_length=50_000)
    replace: str = Field("", max_length=50_000)


class CommentCreate(BaseModel):
    quote: str = Field(..., min_length=1, max_length=50_000)
    body: str = Field("", max_length=50_000)
    author: str = Field("human", max_length=10)
    before_ctx: Optional[str] = Field(None, max_length=200)
    after_ctx: Optional[str] = Field(None, max_length=200)
    proposal: Optional[ProposalPayload] = None


class CommentUpdate(BaseModel):
    body: Optional[str] = Field(None, max_length=50_000)
    state: Optional[str] = Field(None, max_length=10)


def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any) -> JSONResponse:
    """Copied verbatim from `routes/git_routes.py::_error` — see
    `routes/document_links_routes.py` for the same note."""
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _comment_to_dict(c: DocumentComment) -> Dict[str, Any]:
    return {
        "id": c.id,
        "document_id": c.document_id,
        "base_version": c.base_version,
        "quote": c.quote,
        "before_ctx": c.before_ctx,
        "after_ctx": c.after_ctx,
        "structural_pos": c.structural_pos,
        "body": c.body,
        "author": c.author,
        "state": c.state,
        "proposal": ({"find": c.proposal_find, "replace": c.proposal_replace}
                     if c.proposal_find is not None else None),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def setup_document_comments_routes() -> APIRouter:
    router = APIRouter(tags=["document-comments"])

    def _doc_or_404(db, doc_id: str, user: Optional[str]) -> Document:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            raise HTTPException(404, "Document not found")
        _verify_doc_owner(db, doc, user)
        return doc

    def _comment_or_404(db, doc_id: str, comment_id: str) -> DocumentComment:
        c = db.query(DocumentComment).filter(DocumentComment.id == comment_id).first()
        if not c or c.document_id != doc_id:
            raise HTTPException(404, "Comment not found")
        return c

    @router.get("/api/documents/{doc_id}/comments")
    def list_comments(doc_id: str, request: Request, state: str = "") -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            doc = _doc_or_404(db, doc_id, user)
            q = db.query(DocumentComment).filter(DocumentComment.document_id == doc.id)
            if state:
                q = q.filter(DocumentComment.state == state)
            rows = q.order_by(DocumentComment.created_at).all()
            return {"comments": [_comment_to_dict(c) for c in rows]}
        finally:
            db.close()

    @router.post("/api/documents/{doc_id}/comments", status_code=201)
    def create_comment(doc_id: str, req: CommentCreate, request: Request) -> Any:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            doc = _doc_or_404(db, doc_id, user)
            try:
                comment = document_comments.new_comment(
                    doc, quote=req.quote, body=req.body, author=req.author or "human",
                    proposal_find=(req.proposal.find if req.proposal else None),
                    proposal_replace=(req.proposal.replace if req.proposal else None),
                    before_ctx=req.before_ctx, after_ctx=req.after_ctx,
                )
            except document_comments.CommentError as e:
                return _error(400, e.error_class, str(e))
            db.add(comment)
            db.commit()
            db.refresh(comment)
            return {"comment": _comment_to_dict(comment)}
        finally:
            db.close()

    @router.patch("/api/documents/{doc_id}/comments/{comment_id}")
    def update_comment(doc_id: str, comment_id: str, req: CommentUpdate, request: Request) -> Any:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            _doc_or_404(db, doc_id, user)
            comment = _comment_or_404(db, doc_id, comment_id)
            if req.body is not None:
                comment.body = req.body
            if req.state is not None:
                if req.state not in (document_comments.STATE_OPEN, document_comments.STATE_RESOLVED,
                                      document_comments.STATE_ORPHAN):
                    return _error(400, "document_comments.invalid_state", f"unknown state: {req.state}")
                comment.state = req.state
            db.commit()
            db.refresh(comment)
            return {"comment": _comment_to_dict(comment)}
        finally:
            db.close()

    @router.delete("/api/documents/{doc_id}/comments/{comment_id}")
    def delete_comment(doc_id: str, comment_id: str, request: Request) -> Any:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            _doc_or_404(db, doc_id, user)
            comment = _comment_or_404(db, doc_id, comment_id)
            db.delete(comment)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/api/documents/{doc_id}/comments/{comment_id}/accept")
    def accept_comment(doc_id: str, comment_id: str, request: Request) -> Any:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            doc = _doc_or_404(db, doc_id, user)
            comment = _comment_or_404(db, doc_id, comment_id)
            try:
                new_content = document_comments.accept(db, doc, comment)
            except document_comments.BaseChangedError as e:
                db.rollback()
                return _error(409, e.error_class, str(e))
            except document_comments.CommentError as e:
                db.rollback()
                return _error(400, e.error_class, str(e))
            db.commit()
            db.refresh(doc)
            db.refresh(comment)
            return {"document": {"id": doc.id, "current_content": new_content,
                                  "version_count": doc.version_count},
                    "comment": _comment_to_dict(comment)}
        finally:
            db.close()

    return router
