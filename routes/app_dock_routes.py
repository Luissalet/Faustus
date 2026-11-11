"""routes/app_dock_routes.py -- HTTP surface of the App Dock (src/app_dock.py, contract: Hoard Hub docs/APP_DOCK.md).

GET  /api/app-dock/apps               the family's apps from the Hub: id, name, url, state, icon
GET  /api/app-dock/icon/{app}         the app's icon (proxied from the Hub, so the page needs no Hub access)
POST /api/app-dock/open               {session, app}  dock an app (starts it through the Hub when down); emits `open`
POST /api/app-dock/close              {session}
GET  /api/app-dock/state?session=     the docked app, its tools and context
POST /api/app-dock/state              {session, ready?, tools?, context?, app?}  from the host: what the page reported
GET  /api/app-dock/events?session=    server-sent events for the host: open, close, call, changed, cursor
POST /api/app-dock/result             {session, id, ok, result?, error?}  from the host: the answer to a `call`
POST /api/app-dock/call               {session, tool, args?, actor?}  run one tool in the docked page (what the agent tools and the MCP server use)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core.middleware import require_admin
from src import app_dock

HEARTBEAT_S = 15.0


async def _json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def _fail(exc: app_dock.DockError):
    status = {"bad_args": 400, "no_app": 404, "app_not_found": 404, "hub_unavailable": 503, "app_down": 502}.get(exc.code, 400)
    return JSONResponse(status_code=status, content={"error": exc.message, "error_class": "app_dock." + exc.code})


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def setup_app_dock_routes() -> APIRouter:
    router = APIRouter(tags=["app-dock"])

    def guard(request: Request) -> None:
        require_admin(request)

    @router.get("/api/app-dock/apps")
    async def apps(request: Request):
        guard(request)
        return await asyncio.to_thread(app_dock.list_apps)

    @router.get("/api/app-dock/icon/{app_id}")
    async def icon(request: Request, app_id: str):
        guard(request)
        got = await asyncio.to_thread(app_dock.app_icon, app_id)
        if not got:
            return Response(status_code=404)
        return Response(content=got[0], media_type=got[1], headers={"Cache-Control": "private, max-age=3600"})

    @router.post("/api/app-dock/open")
    async def open_(request: Request):
        guard(request)
        body = await _json(request)
        try:
            return await app_dock.open_app(body.get("session"), str(body.get("app") or ""),
                                           wait=float(body.get("wait") or 0))
        except app_dock.DockError as exc:
            return _fail(exc)

    @router.post("/api/app-dock/close")
    async def close_(request: Request):
        guard(request)
        body = await _json(request)
        try:
            return app_dock.close(body.get("session"))
        except app_dock.DockError as exc:
            return _fail(exc)

    @router.get("/api/app-dock/state")
    async def state_get(request: Request, session: str = ""):
        guard(request)
        snap = app_dock.get(session)
        snap["host"] = app_dock.has_host(session)
        return snap

    @router.post("/api/app-dock/state")
    async def state_post(request: Request):
        guard(request)
        body = await _json(request)
        try:
            return app_dock.report(body.get("session"), ready=body.get("ready"), tools=body.get("tools"),
                                   context=body.get("context"), app=body.get("app"), viewport=body.get("viewport"))
        except app_dock.DockError as exc:
            return _fail(exc)

    @router.get("/api/app-dock/events")
    async def events(request: Request, session: str = ""):
        guard(request)
        try:
            sub = app_dock.subscribe(session)
        except app_dock.DockError as exc:
            return _fail(exc)

        async def stream():
            try:
                yield "retry: 2000\n\n"
                yield _sse("hello", {"session": session, "docked": app_dock.get(session)})
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield _sse(item["event"], item["data"])
            finally:
                app_dock.unsubscribe(sub)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/api/app-dock/result")
    async def result(request: Request):
        guard(request)
        body = await _json(request)
        ok = app_dock.resolve(str(body.get("session") or ""), str(body.get("id") or ""), bool(body.get("ok")),
                              body.get("result"), body.get("error"))
        return {"resolved": ok}

    @router.post("/api/app-dock/call")
    async def call_(request: Request):
        guard(request)
        body = await _json(request)
        try:
            sid = app_dock.resolve_session(str(body.get("session") or ""))
            actor = body.get("actor") if isinstance(body.get("actor"), dict) else app_dock.actor_for(str(body.get("session") or ""))
            return await app_dock.call(sid, str(body.get("tool") or ""), body.get("args") if isinstance(body.get("args"), dict) else {}, actor)
        except app_dock.DockError as exc:
            return _fail(exc)

    return router
