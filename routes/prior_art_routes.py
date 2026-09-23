"""routes/prior_art_routes.py — HTTP surface for prior-art verification
(GIT-08). Thin wrappers over `src.prior_art`, same auth as the neighbouring
tool-catalogue routes (`routes/code_graph_routes.py`): `require_admin` on
every endpoint.

POST /api/prior-art/rubric      {idea, stack?, license?, constraints?}
POST /api/prior-art/verify      {slate, target_license?, stack?}
POST /api/prior-art/search      {query, language?, limit?, include_stale?}
GET  /api/prior-art/reports     ?limit=
GET  /api/prior-art/reports/{id}
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import prior_art

logger = logging.getLogger(__name__)


class RubricBody(BaseModel):
    idea: str
    stack: str = ""
    license: str = ""
    constraints: str = ""


class SlateComponent(BaseModel):
    name: str = ""
    verdict: str = "write"
    repos: List[str] = []
    rationale: str = ""


class SlateBody(BaseModel):
    idea: str = ""
    components: List[Dict[str, Any]] = []


class VerifyBody(BaseModel):
    slate: SlateBody
    target_license: str = ""
    stack: str = ""


class SearchBody(BaseModel):
    query: str
    language: str = ""
    limit: int = 8
    include_stale: bool = False


def _guarded(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("prior_art route failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


def setup_prior_art_routes():
    router = APIRouter(prefix="/api/prior-art")

    @router.post("/rubric")
    def rubric(body: RubricBody, request: Request):
        require_admin(request)
        return _guarded(prior_art.rubric, body.idea, stack=body.stack,
                        license=body.license, constraints=body.constraints)

    @router.post("/verify")
    def verify(body: VerifyBody, request: Request):
        require_admin(request)
        owner = getattr(request.state, "current_user", "") or ""
        return _guarded(prior_art.verify, body.slate.model_dump(),
                        target_license=body.target_license, stack=body.stack, owner=str(owner))

    @router.post("/search")
    def search(body: SearchBody, request: Request):
        require_admin(request)
        return _guarded(prior_art.search, body.query, language=body.language,
                        limit=body.limit, include_stale=body.include_stale)

    @router.get("/reports")
    def list_reports(request: Request, limit: int = 20):
        require_admin(request)
        return {"reports": _guarded(prior_art.reports, limit)}

    @router.get("/reports/{report_id}")
    def get_report(report_id: str, request: Request):
        require_admin(request)
        found = _guarded(prior_art.report, report_id)
        if found is None:
            raise HTTPException(status_code=404, detail=f"no such report: {report_id}")
        return found

    return router
