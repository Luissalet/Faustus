"""routes/extraction_routes.py -- HTTP surface of structured extraction (OBJ-24).

POST   /api/extract                 {path | text, schema | schema_name, instructions?, ocr?, tier?, max_chars?}
POST   /api/extract/profile         {schema | schema_name, text? | text_chars?, tier?}  (no model is called)
GET    /api/extract/schemas         the caller's saved schemas
GET    /api/extract/schemas/{name}
PUT    /api/extract/schemas/{name}  {schema, description?}
DELETE /api/extract/schemas/{name}

Every route needs a signed-in user and works on that user's saved schemas
(`DATA_DIR/extraction_schemas/<owner>/<name>.json`). Reading a document by
`path` reads the server's disk, so it is also admin-only, like the other
routes that take a local path. A schema name is letters, digits, `_` and `-`
only: it can never name a file outside the owner's folder.
"""
import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src import schema_extraction as se
from src.auth_helpers import require_user, storage_owner_for_request

logger = logging.getLogger(__name__)

_STATUS = {"unknown_schema": 404, "unreadable": 422, "no_text": 422}


def _error(exc: se.SchemaExtractionError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(exc.code, 400), detail={"error": str(exc), "code": exc.code})


def _owner(request: Request) -> str:
    require_user(request)
    return storage_owner_for_request(request) or ""


def setup_extraction_routes():
    router = APIRouter(prefix="/api/extract")

    @router.post("")
    async def extract(request: Request):
        owner = _owner(request)
        body = await _json(request)
        path, text = body.get("path"), body.get("text")
        if path is not None:
            require_admin(request)
        if (path is None) == (text is None):
            raise HTTPException(status_code=400, detail={"error": "give exactly one of `path` or `text`",
                                                         "code": "invalid_arguments"})
        from src.workflows.model_calls import ModelUnavailable
        try:
            return await se.extract_to_schema(
                owner, path=path, text=text, schema=body.get("schema"), schema_name=body.get("schema_name"),
                instructions=str(body.get("instructions") or ""), ocr=str(body.get("ocr") or "auto"),
                tier=str(body.get("tier") or "auto"), max_chars=body.get("max_chars") or se.DEFAULT_MAX_CHARS,
                timeout_s=float(body.get("timeout_s") or se.DEFAULT_TIMEOUT_S))
        except se.SchemaExtractionError as exc:
            raise _error(exc)
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail={"error": str(exc), "code": "model_unavailable"})

    @router.post("/profile")
    async def profile(request: Request):
        owner = _owner(request)
        body = await _json(request)
        schema = body.get("schema")
        try:
            if schema is None and body.get("schema_name"):
                saved = se.get_schema(owner, str(body["schema_name"]))
                if saved is None:
                    raise se.SchemaExtractionError(f"there is no saved schema called {body['schema_name']!r}",
                                                   "unknown_schema")
                schema = saved["schema"]
            if schema is None:
                raise se.SchemaExtractionError("give a `schema` or a `schema_name`", "invalid_arguments")
            text = body.get("text")
            chars = len(text) if isinstance(text, str) else int(body.get("text_chars") or 0)
            return se.preview_profile(schema, text_chars=chars, owner=owner, tier=str(body.get("tier") or "auto"))
        except se.SchemaExtractionError as exc:
            raise _error(exc)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc), "code": "invalid_arguments"})

    @router.get("/schemas")
    def list_schemas(request: Request):
        return {"schemas": se.list_schemas(_owner(request))}

    @router.get("/schemas/{name}")
    def get_schema(name: str, request: Request):
        owner = _owner(request)
        try:
            record = se.get_schema(owner, name)
        except se.SchemaExtractionError as exc:
            raise _error(exc)
        if record is None:
            raise HTTPException(status_code=404, detail={"error": f"no saved schema called {name!r}",
                                                         "code": "unknown_schema"})
        return record

    @router.put("/schemas/{name}")
    async def put_schema(name: str, request: Request):
        owner = _owner(request)
        body = await _json(request)
        try:
            return se.save_schema(owner, name, body.get("schema"), str(body.get("description") or ""))
        except se.SchemaExtractionError as exc:
            raise _error(exc)

    @router.delete("/schemas/{name}")
    def delete_schema(name: str, request: Request):
        owner = _owner(request)
        try:
            removed = se.delete_schema(owner, name)
        except se.SchemaExtractionError as exc:
            raise _error(exc)
        if not removed:
            raise HTTPException(status_code=404, detail={"error": f"no saved schema called {name!r}",
                                                         "code": "unknown_schema"})
        return {"deleted": name}

    return router


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail={"error": "the body must be a JSON object",
                                                     "code": "invalid_arguments"})
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"error": "the body must be a JSON object",
                                                     "code": "invalid_arguments"})
    return body
