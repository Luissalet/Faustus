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

GET    /api/panels/templates                ready-made panels (src/panel_templates)
POST   /api/panels/from-template            {template, name?, id?}  create one (page, tables, scheduled actions, first run)
POST   /api/panels/fetch                    {url, method?, headers?, body?, timeout_s?}  what faustus.fetch() calls:
                                            /api/... of Faustus, hub:/api/... of the Hub or any http(s) URL, from the server
POST   /api/panels/hub                      {path, method?, body?, timeout_s?}  what faustus.hub() calls: the Hoard Hub's routes
                                            (the Ágora with spaces and tasks, apps, placement), wherever the Hub runs
GET    /api/panels/sources?app=&probe=      what a panel can read: Faustus routes, Hub routes, Hoards and their tools
POST   /api/panels/{id}/db/query            {sql, params?, limit?, offset?, timeout_s?}  rows of one statement, a page at a time
POST   /api/panels/{id}/db/exec             {sql, params?} | {sql, many: [[..]]} | {batch: [{sql, params}]}  one transaction
GET    /api/panels/{id}/db/tables           tables and views with columns and row counts
GET    /api/panels/{id}/actions             server actions with their schedule, last run and next run
PUT    /api/panels/{id}/actions/{name}      {tool|url, args?, app?, every_minutes?|cron?, save?, enabled?}  create or replace
DELETE /api/panels/{id}/actions/{name}
POST   /api/panels/{id}/actions/{name}/run  run it now and save the answer

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
    cls = exc.error_class
    status = (404 if cls.endswith("not_found") else 409 if cls.endswith("exists") else
              502 if cls in ("panel.action_failed", "panel.fetch_failed") else 400)
    return JSONResponse(status_code=status, content={"error": exc.detail, "error_class": cls})


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
                from src import panel_actions
                from src.hoard_link import family
                panel_actions.family_ready()  # configured once every few minutes, not on every call
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

    @router.get("/api/panels/templates")
    async def templates(request: Request):
        guard(request)
        from src import panel_templates
        return {"templates": panel_templates.list_templates()}

    @router.post("/api/panels/from-template")
    async def from_template(request: Request):
        guard(request)
        from src import panel_templates
        body = await _json(request)
        try:
            return await panel_templates.create_from_template(
                body.get("template"), name=body.get("name") or None, panel_id=body.get("id") or None,
                owner=effective_user(request) or None, first_run=body.get("first_run") is not False)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/fetch")
    async def panel_fetch(request: Request):
        guard(request)
        from src import panel_actions
        body = await _json(request)
        try:
            timeout = float(body.get("timeout_s") or panel_actions.DEFAULT_TIMEOUT_S)
            headers = body.get("headers") if isinstance(body.get("headers"), dict) else None
            return await asyncio.to_thread(panel_actions.fetch, body.get("url"), method=body.get("method") or "GET",
                                           body=body.get("body"), headers=headers, timeout_s=max(1.0, min(timeout, 600.0)))
        except panels.PanelError as exc:
            return _fail(exc)
        except (TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"error": str(exc), "error_class": "panel.fetch_bad_args"})

    @router.post("/api/panels/hub")
    async def panel_hub(request: Request):
        guard(request)
        from src import panel_actions
        body = await _json(request)
        path = str(body.get("path") or "").strip()
        if not path.startswith("/"):
            return JSONResponse(status_code=400, content={"error": "`path` is the Hub's path, e.g. /api/agora/spaces",
                                                          "error_class": "panel.fetch_bad_args"})
        try:
            timeout = float(body.get("timeout_s") or panel_actions.DEFAULT_TIMEOUT_S)
            return await asyncio.to_thread(panel_actions.fetch, "hub:" + path, method=body.get("method") or "GET",
                                           body=body.get("body"), timeout_s=max(1.0, min(timeout, 600.0)))
        except panels.PanelError as exc:
            return _fail(exc)
        except (TypeError, ValueError) as exc:
            return JSONResponse(status_code=400, content={"error": str(exc), "error_class": "panel.fetch_bad_args"})

    @router.get("/api/panels/sources")
    async def panel_sources_route(request: Request, app: str = "", probe: str = "", tools: bool = True):
        guard(request)
        from src import panel_sources
        try:
            return await asyncio.to_thread(panel_sources.sources, app=app or None, probe_path=probe or None, with_tools=tools)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/{panel_id}/db/query")
    async def db_query(panel_id: str, request: Request):
        guard(request)
        from src import panel_db
        body = await _json(request)
        try:
            return await asyncio.to_thread(panel_db.query, panel_id, body.get("sql"), body.get("params"),
                                           limit=body.get("limit"), offset=body.get("offset") or 0,
                                           timeout_s=body.get("timeout_s"))
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/{panel_id}/db/exec")
    async def db_exec(panel_id: str, request: Request):
        guard(request)
        from src import panel_db
        body = await _json(request)
        try:
            return await asyncio.to_thread(panel_db.exec_, panel_id, body.get("sql"), body.get("params"),
                                           many=body.get("many"), batch=body.get("batch"), timeout_s=body.get("timeout_s"))
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/db/tables")
    async def db_tables(panel_id: str, request: Request):
        guard(request)
        from src import panel_db
        try:
            return await asyncio.to_thread(panel_db.tables, panel_id)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.get("/api/panels/{panel_id}/actions")
    async def actions_list(panel_id: str, request: Request):
        guard(request)
        from src import panel_actions
        try:
            return await asyncio.to_thread(panel_actions.list_actions, panel_id)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.put("/api/panels/{panel_id}/actions/{name}")
    async def actions_set(panel_id: str, name: str, request: Request):
        guard(request)
        from src import panel_actions
        body = await _json(request)
        try:
            return await asyncio.to_thread(panel_actions.set_action, panel_id, body, name=name,
                                           owner=effective_user(request) or None)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.delete("/api/panels/{panel_id}/actions/{name}")
    async def actions_delete(panel_id: str, name: str, request: Request):
        guard(request)
        from src import panel_actions
        try:
            return await asyncio.to_thread(panel_actions.remove_action, panel_id, name)
        except panels.PanelError as exc:
            return _fail(exc)

    @router.post("/api/panels/{panel_id}/actions/{name}/run")
    async def actions_run(panel_id: str, name: str, request: Request):
        guard(request)
        from src import panel_actions
        try:
            return await panel_actions.run_action(panel_id, name, owner=effective_user(request) or None)
        except panels.PanelError as exc:
            return _fail(exc)

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
