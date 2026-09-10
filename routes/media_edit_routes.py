"""MEDIA-03 — non-destructive image editing, over HTTP.

Minimal, real endpoints on top of `src.media_edit_projects`: open a project
on an existing file, add non-destructive layers/ops, and export a flat
result explicitly. There is no endpoint that writes the original — `export`
always names a destination, and the module itself refuses to silently
replace the source (see `media_edit_projects.export`'s
`would_overwrite_original` / `destination_exists` refusals).

A SEPARATE router from `routes/media_routes.py` on purpose:
`tests/test_media_routes.py::test_there_is_no_route_that_accepts_a_graph`
asserts an exact allowlist of paths under `setup_media_routes()` as "the
claim of the phase" (Phase 3 never grows an endpoint that takes a ComfyUI
graph) — these routes accept image bytes and layer offsets, not a graph, so
they do not violate that claim, but adding them to that router would still
break the test's literal path list, and that test file is outside this
batch's file list. Registering this module separately keeps
`routes/media_routes.py` byte-for-byte as that lot left it.

NOT YET WIRED into `app.py` (out of this batch's file list) — see the final
report for the one `include_router` line that exposes it.
"""

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin

from routes.media_routes import _json_object, _current_user


def _edit_error(exc) -> None:
    status = (404 if exc.code == "not_found" else
             409 if exc.code in ("destination_exists", "would_overwrite_original", "source_changed") else
             400)
    raise HTTPException(status_code=status, detail={"reason": exc.code, "message": str(exc)})


def _edit_owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    owner = effective_storage_owner(_current_user(request), auth_is_disabled=mw.auth_disabled())
    if not owner:
        raise HTTPException(400, "An edit project needs an owner")
    return owner


def setup_media_edit_routes():
    router = APIRouter(prefix="/api/media/edit", tags=["media"])

    @router.post("/projects")
    async def create_edit_project(request: Request):
        require_admin(request)
        payload = await _json_object(request)
        from src import media_edit_projects
        try:
            return media_edit_projects.create(str(payload.get("source_path") or ""),
                                              owner=_edit_owner(request))
        except media_edit_projects.EditProjectError as e:
            _edit_error(e)

    @router.get("/projects/{project_id}")
    def get_edit_project(project_id: str, request: Request):
        require_admin(request)
        from src import media_edit_projects
        try:
            return media_edit_projects.get(project_id, owner=_edit_owner(request))
        except media_edit_projects.EditProjectError as e:
            _edit_error(e)

    @router.post("/projects/{project_id}/layers")
    async def add_edit_layer(project_id: str, request: Request):
        require_admin(request)
        payload = await _json_object(request)
        from src import media_edit_projects
        try:
            return media_edit_projects.add_layer(
                project_id, owner=_edit_owner(request),
                image_b64=str(payload.get("image_b64") or ""),
                x=int(payload.get("x") or 0), y=int(payload.get("y") or 0),
                mask_b64=str(payload.get("mask_b64") or ""),
                label=str(payload.get("label") or ""))
        except media_edit_projects.EditProjectError as e:
            _edit_error(e)

    @router.post("/projects/{project_id}/ops")
    async def add_edit_op(project_id: str, request: Request):
        require_admin(request)
        payload = await _json_object(request)
        from src import media_edit_projects
        try:
            return media_edit_projects.record_op(
                project_id, owner=_edit_owner(request),
                op_type=str(payload.get("type") or ""), params=payload.get("params") or {})
        except media_edit_projects.EditProjectError as e:
            _edit_error(e)

    @router.post("/projects/{project_id}/export")
    async def export_edit_project(project_id: str, request: Request):
        require_admin(request)
        payload = await _json_object(request)
        from src import media_edit_projects
        try:
            return media_edit_projects.export(
                project_id, str(payload.get("path") or ""), owner=_edit_owner(request),
                confirm_overwrite_original=bool(payload.get("confirm_overwrite_original", False)))
        except media_edit_projects.EditProjectError as e:
            _edit_error(e)

    return router
