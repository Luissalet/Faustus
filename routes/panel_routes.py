"""routes/panel_routes.py -- HTTP surface for user panels (src/panels.py).

GET    /api/panels                          the panels, in sidebar order
POST   /api/panels                          {name, html, icon?, description?, id?}  create
POST   /api/panels/reorder                  {ids: [...]}  new sidebar order
POST   /api/panels/tool                     {tool, args?, app?}  what faustus.tool() calls (see below)
GET    /api/panels/faustus-panel.js         the helper script a panel includes
GET    /api/panels/{id}                     metadata and html
PUT    /api/panels/{id}                     {html?, name?, icon?, description?, order?}  (a new html is a new version)
DELETE /api/panels/{id}
GET    /api/panels/{id}/page                the page itself, served on this origin for the iframe
GET    /api/panels/{id}/versions            saved versions
GET    /api/panels/{id}/versions/{n}        the html of version n (text/plain)
POST   /api/panels/{id}/restore             {version}
POST   /api/panels/{id}/check               render headless; console errors, failed requests, screenshot
GET    /api/panels/{id}/check.png           the last screenshot
GET    /api/panels/{id}/storage             every key
GET|PUT|DELETE /api/panels/{id}/storage/{key}   PUT {value}

`faustus.tool(name, args, app)`: with `app` the call goes to that connected Hoard through the Hub
(same path the agent uses); without it, to the agent's native tool of that name. Panels are the
owner's own pages, so every tool is available to them.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from core.middleware import require_admin
from src import panels
from src.auth_helpers import effective_user


def _fail(exc: panels.PanelError):
    status = 404 if exc.error_class.endswith("not_found") else 409 if exc.error_class.endswith("exists") else 400
    return JSONResponse(status_code=status, content={"error": exc.detail, "error_class": exc.error_class})


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def setup_panel_routes() -> APIRouter:
    router = APIRouter(tags=["panels"])

    def guard(request: Request) -> None:
        require_admin(request)

    @router.get("/api/panels")
    async def list_panels(request: Request):
        guard(request)
        return {"panels": panels.list_panels()}

    @router.post("/api/panels")
    async def create_panel(request: Request):
        guard(request)
        body = await _json(request)
        try:
            return panels.create(body.get("name"), body.get("html"), icon=body.get("icon"),
                                 description=body.get("description", ""), panel_id=body.get("id") or None)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/reorder")
    async def reorder(request: Request):
        guard(request)
        ids = (await _json(request)).get("ids")
        if not isinstance(ids, list):
            return JSONResponse(status_code=400, content={"error": "`ids` must be a list", "error_class": "panel.bad_args"})
        return {"panels": panels.reorder([str(i) for i in ids])}

    @router.get("/api/panels/faustus-panel.js")
    async def helper_js():
        return Response(panels.PANEL_HELPER_JS, media_type="application/javascript", headers={"Cache-Control": "no-cache"})

    @router.post("/api/panels/tool")
    async def panel_tool(request: Request):
        guard(request)
        body = await _json(request)
        name = str(body.get("tool") or "").strip()
        args = body.get("args") if isinstance(body.get("args"), dict) else {}
        app = str(body.get("app") or "").strip()
        if not name:
            return JSONResponse(status_code=400, content={"error": "`tool` is required", "error_class": "panel.bad_args"})
        if app:
            def call():
                from src import family_services
                from src.hoard_link import family
                family_services.configure()
                return family.call(app, name, dict(args), timeout=60.0)
            try:
                res = await asyncio.to_thread(call)
            except Exception as exc:  # noqa: BLE001
                return JSONResponse(status_code=502, content={"error": f"{type(exc).__name__}: {exc}", "error_class": "panel.hoard_unreachable"})
            if isinstance(res, dict) and (res.get("ok") is False or res.get("error")):
                return JSONResponse(status_code=502, content={"error": str(res.get("error") or f"HTTP {res.get('status')}"),
                                                              "error_class": "panel.hoard_error"})
            return {"app": app, "tool": name, "result": res.get("result", res) if isinstance(res, dict) else res}
        from src.agent_tools import TOOL_HANDLERS
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return JSONResponse(status_code=404, content={"error": f"no agent tool named {name}", "error_class": "panel.not_found"})
        ctx = {"owner": effective_user(request) or "", "source": "panel"}
        out = await handler(json.dumps(args), ctx)
        if isinstance(out, dict) and out.get("error"):
            return JSONResponse(status_code=400, content={"error": str(out["error"]), "error_class": out.get("error_class") or "panel.tool_error"})
        return {"tool": name, "output": out.get("output") if isinstance(out, dict) else out,
                "result": out.get("result") if isinstance(out, dict) else None}

    @router.get("/api/panels/{panel_id}")
    async def get_panel(panel_id: str, request: Request):
        guard(request)
        try:
            return panels.get(panel_id)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.put("/api/panels/{panel_id}")
    async def update_panel(panel_id: str, request: Request):
        guard(request)
        body = await _json(request)
        try:
            return panels.update(panel_id, html=body.get("html"), name=body.get("name"), icon=body.get("icon"),
                                 description=body.get("description"), order=body.get("order"))
        except panels.PanelError as exc:
            return _fail(exc)

    @router.delete("/api/panels/{panel_id}")
    async def delete_panel(panel_id: str, request: Request):
        guard(request)
        try:
            return panels.delete(panel_id)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/page")
    async def page(panel_id: str, request: Request):
        guard(request)
        try:
            return HTMLResponse(panels.page_html(panel_id), headers={"Cache-Control": "no-store"})
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/versions")
    async def versions(panel_id: str, request: Request):
        guard(request)
        try:
            return panels.versions(panel_id)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/versions/{n}")
    async def version_html(panel_id: str, n: int, request: Request):
        guard(request)
        try:
            return PlainTextResponse(panels.version_html(panel_id, n))
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/{panel_id}/restore")
    async def restore(panel_id: str, request: Request):
        guard(request)
        try:
            return panels.restore(panel_id, (await _json(request)).get("version"))
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/{panel_id}/check")
    async def check(panel_id: str, request: Request):
        guard(request)
        from core.middleware import INTERNAL_TOOL_TOKEN
        body = await _json(request)
        base = f"{request.url.scheme}://{request.url.netloc}"
        try:
            return await asyncio.to_thread(panels.check, panel_id, base_url=base, wait_ms=int(body.get("wait_ms") or 1500),
                                           token=INTERNAL_TOOL_TOKEN)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/check.png")
    async def check_png(panel_id: str, request: Request):
        guard(request)
        try:
            path = panels._existing(panel_id) / "check.png"
        except panels.PanelError as exc:
            return _fail(exc)
        if not path.is_file():
            return JSONResponse(status_code=404, content={"error": "no screenshot yet; run a check", "error_class": "panel.not_found"})
        return FileResponse(str(path), media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/api/panels/{panel_id}/storage")
    async def storage_all(panel_id: str, request: Request):
        guard(request)
        try:
            return panels.storage_list(panel_id)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/storage/{key}")
    async def storage_get(panel_id: str, key: str, request: Request):
        guard(request)
        try:
            return {"key": key, "value": panels.storage_get(panel_id, key)}
        except panels.PanelError as exc:
            return _fail(exc)

    @router.put("/api/panels/{panel_id}/storage/{key}")
    async def storage_put(panel_id: str, key: str, request: Request):
        guard(request)
        try:
            return panels.storage_set(panel_id, key, (await _json(request)).get("value"))
        except panels.PanelError as exc:
            return _fail(exc)

    @router.delete("/api/panels/{panel_id}/storage/{key}")
    async def storage_del(panel_id: str, key: str, request: Request):
        guard(request)
        try:
            return panels.storage_delete(panel_id, key)
        except panels.PanelError as exc:
            return _fail(exc)

    return router
