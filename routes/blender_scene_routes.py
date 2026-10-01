"""routes/blender_scene_routes.py -- HTTP surface for typed 3D scenes rendered
with Blender. Thin wrappers over `src.blender_scene`, same auth as the
neighbouring tool-catalogue routes: `require_admin` on every endpoint.

GET  /api/blender/probe
GET  /api/blender/schema     ?format=summary|json_schema&op=
POST /api/blender/validate   {scene | ops [, meta], folder?}
POST /api/blender/run        {scene | ops [, meta], folder?, timeout?}

A run writes only inside `BLENDER_SCENES_DIR/api[/<folder>]`; a scene cannot
name a path outside that folder.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src import blender_scene
from src.agent_tools import blender_scene_tool as tool

logger = logging.getLogger(__name__)


class SceneBody(BaseModel):
    scene: Optional[Any] = None
    ops: Optional[List[Dict[str, Any]]] = None
    meta: Optional[Dict[str, Any]] = None
    folder: str = ""
    timeout: Optional[float] = None


def _scene(body: SceneBody):
    if body.scene is not None:
        return body.scene
    if body.ops is not None:
        out: Dict[str, Any] = {"ops": body.ops}
        if body.meta:
            out["meta"] = body.meta
        return out
    raise HTTPException(status_code=400, detail="provide `scene` (an object with `ops`, or a list of ops) or `ops`")


def _root(folder: str) -> str:
    return tool.session_root({"session_id": "api"}, folder)


def setup_blender_scene_routes():
    router = APIRouter(prefix="/api/blender")

    @router.get("/probe")
    def probe(request: Request):
        require_admin(request)
        return blender_scene.probe()

    @router.get("/schema")
    def schema(request: Request, format: str = Query("summary"), op: str = ""):
        require_admin(request)
        if op and op not in blender_scene.OPS:
            raise HTTPException(status_code=400, detail=f"unknown op '{op}'")
        if format == "json_schema":
            return blender_scene.scene_schema()
        if format != "summary":
            raise HTTPException(status_code=400, detail="format must be summary or json_schema")
        return {"summary": blender_scene.scene_summary(op or None), "ops": list(blender_scene.OPS)}

    @router.post("/validate")
    def validate(body: SceneBody, request: Request):
        require_admin(request)
        return blender_scene.validate_scene(_scene(body), _root(body.folder)).to_dict()

    @router.post("/run")
    def run(body: SceneBody, request: Request):
        require_admin(request)
        scene = _scene(body)
        try:
            return blender_scene.run_scene(scene, _root(body.folder), timeout=body.timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("blender run failed: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

    return router
