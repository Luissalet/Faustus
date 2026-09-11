"""Wiki-links and backlinks between Documents (ADP-06, W1-E) —
`/api/documents/{doc_id}/links`, `/api/documents/{doc_id}/backlinks`,
`/api/documents/links/rebuild`.

Owner-scoped the same way `routes/document/document_helpers.py` gates every
other document route: `Document` has no `project_id` of its own (only
`session_id` and its own `owner` column — see `core/database.py::Document`),
so unlike `routes/board_routes.py` this is NOT nested under
`/api/projects/{project_id}/...`. CONTRATO_ADP_W1's ficha for ADP-06
sketches a `POST /api/projects/{id}/documents/links/rebuild` route, but that
project-scoped shape does not exist for documents in this repo (confirmed:
`ADP01_INVENTARIO.md` ADP-06 row, and no `project_id` column on `Document`)
— rebuilding is owner-scoped instead (`POST /api/documents/links/rebuild`),
which is the real boundary `resolve()` enforces in `src/document_links.py`.
Flagged in the W1-E report for the orchestrator.

`src/document_links.py` owns parsing/resolution/storage; this module is
thin HTTP plumbing over it, matching `routes/document/document_helpers.py`'s
own `get_current_user` + `_verify_doc_owner` gate rather than
`board_routes.py`'s `require_user`/`effective_user` (there is no project
here to key auth off of — a document's owner column is the only identity).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from core.database import SessionLocal, Document
from routes.document.document_helpers import _verify_doc_owner
from src.auth_helpers import get_current_user
from src import document_links

logger = logging.getLogger(__name__)


def _error(status: int, error_class: str, detail: Optional[str] = None, **extra: Any):
    """Copied verbatim from `routes/git_routes.py::_error` /
    `routes/board_routes.py::_error` — flat body, `error_class` at the top
    level (CONTRATO_ADP_W1's own error-shape rule)."""
    from fastapi.responses import JSONResponse
    body: Dict[str, Any] = {"error_class": error_class}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def setup_document_links_routes() -> APIRouter:
    router = APIRouter(tags=["document-links"])

    def _doc_or_404(db, doc_id: str, user: Optional[str]) -> Document:
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc:
            raise HTTPException(404, "Document not found")
        _verify_doc_owner(db, doc, user)
        return doc

    @router.get("/api/documents/{doc_id}/links")
    def get_links(doc_id: str, request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            doc = _doc_or_404(db, doc_id, user)
            return {"document_id": doc.id, "links": document_links.get_links(doc.id)}
        finally:
            db.close()

    @router.get("/api/documents/{doc_id}/backlinks")
    def get_backlinks(doc_id: str, request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            doc = _doc_or_404(db, doc_id, user)
            return {"document_id": doc.id, "backlinks": document_links.get_backlinks(doc.id, doc.owner)}
        finally:
            db.close()

    @router.post("/api/documents/links/rebuild")
    def rebuild_links(request: Request) -> Dict[str, Any]:
        """Recompute the whole index for the caller's own documents. The
        index is defined to be reconstructible from `current_content` alone
        (see `src/document_links.py` module docstring) — this route exists
        for exactly that: a deleted/corrupt `document_links.sqlite3`, or
        picking up documents written before this index existed."""
        user = get_current_user(request)
        if user is None:
            from src.auth_helpers import _auth_disabled
            if not _auth_disabled():
                raise HTTPException(403, "Authentication required")
        db = SessionLocal()
        try:
            count = document_links.rebuild_all_for_owner(db, user)
            db.commit()
            return {"documents_processed": count}
        finally:
            db.close()

    return router
