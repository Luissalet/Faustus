"""routes/doc_lint_routes.py -- HTTP surface for the report-only Markdown
linter (src/doc_lint.py). Admin only, like the neighbouring doc-claims route.

GET  /api/doc-lint?paths=README.md,FAUSTUS.md&rules=headings,dates
POST /api/doc-lint   {"paths": [...], "rules": [...]}
GET  /api/doc-lint/rules

Every path goes through the workspace guard the file tools use
(`_resolve_search_root`): a path outside the allowed roots (or a sensitive
one) answers 403 and nothing is read. Unknown rules answer 400.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import doc_lint

logger = logging.getLogger(__name__)

MAX_PATHS = 20


def _split(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _run(paths: List[str], rules: Optional[List[str]]) -> Dict[str, Any]:
    if not paths:
        raise HTTPException(status_code=400, detail="paths is required")
    if len(paths) > MAX_PATHS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_PATHS} paths per request")
    from src.tool_execution import _resolve_search_root
    for p in paths:  # refuse the whole request before reading anything
        try:
            _resolve_search_root(p)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=f"refused: {exc}")
    try:
        return doc_lint.lint_paths(paths, rules or None, resolver=_resolve_search_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("doc_lint route failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


def setup_doc_lint_routes():
    router = APIRouter(prefix="/api/doc-lint")

    @router.get("/rules")
    def get_rules(request: Request):
        require_admin(request)
        return doc_lint.rules_catalogue()

    @router.get("")
    def get_lint(request: Request, paths: str = "", rules: str = ""):
        require_admin(request)
        return _run(_split(paths), _split(rules))

    @router.post("")
    async def post_lint(request: Request):
        require_admin(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = None
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON object body required")
        return _run(_split(body.get("paths") or body.get("path")), _split(body.get("rules")))

    return router
