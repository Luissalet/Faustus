"""routes/visual_index_routes.py -- HTTP surface of the local visual index (src/visual_index/).

GET    /api/visual-index/status[?probe=1]           embedder (mode, python, device) / vision / limits / collections
GET    /api/visual-index/collections                the caller's collections
GET    /api/visual-index/collections/{name}         its sources
DELETE /api/visual-index/collections/{name}         ?source=<path or URL> removes one source, else the whole collection
POST   /api/visual-index/add                        {sources | paths | urls, collection?, pages?, force?}
POST   /api/visual-index/search                     {query, k?, collection?}
GET    /api/visual-index/tiles/{id}                 one strip's metadata
GET    /api/visual-index/tiles/{id}/image           the strip as PNG
POST   /api/visual-index/tiles/{id}/read            {question?} the resident vision model reads the strip

Every route needs a signed-in user and works on that user's collections. Local file paths are read from the server's disk,
so adding a path is admin-only (URLs are not). Handlers are plain ``def`` (run in the thread pool): indexing is slow.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.middleware import require_admin
from src.auth_helpers import require_user, storage_owner_for_request
from src.visual_index import service as svc
from src.visual_index.embedder import EmbedderUnavailable
from src.visual_index.render import is_url

logger = logging.getLogger(__name__)

_STATUS = {"unknown_collection": 404, "unknown_strip": 404, "unknown_source": 404, "disabled": 403, "dependencies_missing": 503,
           "dependencies_broken": 503, "load_failed": 503, "embedder_unavailable": 503, "embedder_mismatch": 409}

_JSON_OBJECT = {"type": "object", "additionalProperties": True}


def _body(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema: Dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": True}
    if required:
        schema["required"] = required
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


_ADD_OPENAPI = _body({
    "sources": {"type": "array", "items": {"type": "string"}, "description": "Paths (admin) and/or http(s) URLs: PDF, picture or web page."},
    "paths": {"type": "array", "items": {"type": "string"}},
    "urls": {"type": "array", "items": {"type": "string"}},
    "collection": {"type": "string", "description": "Collection name, default 'default'."},
    "pages": {"type": "string", "description": "PDF pages, e.g. '1-5,9'."},
    "force": {"type": "boolean"},
})
_SEARCH_OPENAPI = _body({
    "query": {"type": "string"}, "k": {"type": "integer"}, "collection": {"type": "string"}}, required=["query"])
_READ_OPENAPI = _body({"question": {"type": "string"}})


def _owner(request: Request) -> Optional[str]:
    require_user(request)
    return storage_owner_for_request(request)


def _error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "invalid_arguments")
    return HTTPException(status_code=_STATUS.get(code, 400), detail={"error": str(exc), "message": str(exc), "code": code})


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": "the body must be a JSON object", "message": "the body must be a JSON object",
                                                         "code": "invalid_arguments"})
    return body


def _strings(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def setup_visual_index_routes():
    router = APIRouter(prefix="/api/visual-index")

    @router.get("/status")
    def status(request: Request, probe: bool = False):
        return svc.get_service().status(_owner(request), probe=probe)

    @router.get("/collections")
    def collections(request: Request):
        return svc.get_service().list(_owner(request))

    @router.get("/collections/{name}")
    def collection(name: str, request: Request):
        try:
            return svc.get_service().list(_owner(request), name)
        except svc.VisualIndexError as exc:
            raise _error(exc)

    @router.delete("/collections/{name}")
    def delete_collection(name: str, request: Request, source: Optional[str] = None):
        owner = _owner(request)
        try:
            return svc.get_service().remove(owner, name, source)
        except svc.VisualIndexError as exc:
            raise _error(exc)

    @router.post("/add", openapi_extra=_ADD_OPENAPI)
    async def add(request: Request):
        owner = _owner(request)
        body = await _json(request)
        sources = _strings(body.get("sources")) + _strings(body.get("paths")) + _strings(body.get("urls"))
        if any(not is_url(s) for s in sources):
            require_admin(request)
        from starlette.concurrency import run_in_threadpool
        try:
            return await run_in_threadpool(
                svc.get_service().add, owner, sources, collection=str(body.get("collection") or "default"),
                pages=body.get("pages"), force=bool(body.get("force")))
        except (svc.VisualIndexError, EmbedderUnavailable) as exc:
            raise _error(exc)

    @router.post("/search", openapi_extra=_SEARCH_OPENAPI)
    async def search(request: Request):
        owner = _owner(request)
        body = await _json(request)
        from starlette.concurrency import run_in_threadpool
        try:
            return await run_in_threadpool(svc.get_service().search, owner, str(body.get("query") or ""),
                                           k=int(body.get("k") or 5), collection=body.get("collection") or None)
        except (svc.VisualIndexError, EmbedderUnavailable) as exc:
            raise _error(exc)

    @router.get("/tiles/{tile_id}")
    def tile(tile_id: str, request: Request):
        try:
            return svc.get_service().get_tile(_owner(request), tile_id)
        except svc.VisualIndexError as exc:
            raise _error(exc)

    @router.get("/tiles/{tile_id}/image")
    def tile_image(tile_id: str, request: Request):
        try:
            info = svc.get_service().get_tile(_owner(request), tile_id)
        except svc.VisualIndexError as exc:
            raise _error(exc)
        return FileResponse(info["path"], media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    @router.post("/tiles/{tile_id}/read", openapi_extra=_READ_OPENAPI)
    async def read_tile(tile_id: str, request: Request):
        owner = _owner(request)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        from starlette.concurrency import run_in_threadpool
        try:
            return await run_in_threadpool(svc.get_service().read_tile, owner, tile_id,
                                           str((body or {}).get("question") or ""))
        except svc.VisualIndexError as exc:
            raise _error(exc)

    return router
