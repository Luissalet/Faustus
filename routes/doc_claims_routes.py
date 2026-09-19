"""routes/doc_claims_routes.py — HTTP surface for the doc-claim drift
checker (src/doc_claims.py). Same auth style as the neighbouring code-graph
routes (`routes/code_graph_routes.py`): `require_admin` on the endpoint,
workspace resolved through the same active-workspace guard other admin
tool routes use.

GET /api/doc-claims   ?docs=FAUSTUS.md,README.md&root=&json=true
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import doc_claims

logger = logging.getLogger(__name__)


def setup_doc_claims_routes():
    router = APIRouter(prefix="/api/doc-claims")

    @router.get("")
    def get_report(request: Request, docs: str = "", root: str = ""):
        require_admin(request)
        try:
            from src.code_graph.query import _root
            ws = _root(root)
        except Exception:  # noqa: BLE001 - fall back to a literal root
            ws = root or "."
        doc_list = [d.strip() for d in docs.split(",") if d.strip()] or None
        try:
            data = doc_claims.report(ws, doc_list)
        except Exception as exc:  # noqa: BLE001
            logger.warning("doc_claims route failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
        return data

    return router
