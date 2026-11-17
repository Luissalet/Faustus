"""The Agora for Studio, proxied from the family hub.

The hub owns the Agora (spaces, channels, threads, tasks, agents); Studio draws it as servers and channels. The
browser never calls the hub cross-origin: these routes forward to it with the hub's token and say that the request
comes from the person signed in here, so what is written from Studio is written as the person and what is read
counts as read by them.

* ``GET  /api/agora/x/<path>``  -> hub ``GET  /api/agora/<path>`` (servers, guild, channels, channels/<slug>, feed,
  tasks/<id>, threads/<id>, decisions, locks, console/overview, console/usage…)
* ``POST /api/agora/x/<op>``    -> hub ``POST /api/agora/<op>`` (channel_post, channel_create, task_add, task_edit,
  task_review, resolve, escalate, space_meta_set, console/message…)
* ``GET  /api/agora/pulse``     -> the hub's change token, waiting up to 20 s for a change
* ``GET  /api/agora/icon/<app>``-> the icon of a family app (``hub`` is the hub's own), made small: the originals weigh
  up to 1 MB each and a server list draws thirty of them at 48 px
* ``GET  /api/agora/where``     -> whether the hub is on this machine or on another one, reached through its link
  (``src/hub_link.py``): folders are then chosen on the hub's machine, where the agents work
* ``GET  /api/agora/file/<id>`` -> a file a message carries; ``GET /api/agora/space-icon/<space>`` -> a server's own
  picture. Files come as the hub gives them: shown in place only when it is a picture, plain text or a PDF, a
  download otherwise, and never sniffed, so nothing an agent attaches ever runs as a page of this app

A hub that is down answers 200 with ``{"ok": false, "error": ...}`` so the page can say so and keep its draft.
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core.middleware import require_admin
from src import hub_link

PERSON_SURFACE = "faustus-studio"
_PATH = re.compile(r"^[a-z0-9_][a-z0-9_/\-]{0,120}$")
_APP = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
_FILE = re.compile(r"^[0-9a-f]{32}$")
_SPACE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
_MAX_BYTES = 12_000_000
_ICON_PX = 128
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_icons: Dict[str, Tuple[bytes, str]] = {}
# Apps the hub has no icon for (a server named after no app): asked once, so a list of servers does not ask again on
# every visit. A hub that was not answering is not remembered.
_no_icon: set = set()


def hub_request(method: str, path: str, *, query: Optional[Dict[str, str]] = None, body: Optional[dict] = None,
                timeout: float = 30.0, raw: bool = False, with_headers: bool = False) -> Tuple[Any, ...]:
    """``(status, payload, content_type)``; status None when the hub did not answer. Never raises.
    ``with_headers`` adds a fourth item, the answer's headers."""
    where = hub_link.location()
    token = hub_link.token(where)
    headers = {"Accept": "*/*" if raw else "application/json", "X-Hoard-Person": PERSON_SURFACE,
               "User-Agent": "faustus-agora"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    url = where["url"] + path + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    answer: Dict[str, str] = {}
    more = (lambda *items: (*items, answer)) if with_headers else (lambda *items: items)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            blob = resp.read(_MAX_BYTES)
            status, ctype = resp.status, resp.headers.get("Content-Type", "")
            answer.update({k: v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as exc:
        try:
            blob = exc.read(_MAX_BYTES)
        except Exception:  # noqa: BLE001
            blob = b""
        status, ctype = exc.code, exc.headers.get("Content-Type", "") if exc.headers else ""
    except Exception:  # noqa: BLE001 - refused, timed out, bad URL
        return more(None, None, "")
    if raw:
        return more(status, blob, ctype)
    try:
        return more(status, (json.loads(blob.decode("utf-8")) if blob else None), ctype)
    except (ValueError, UnicodeDecodeError):
        return more(status, None, ctype)


def small_icon(blob: bytes, ctype: str) -> Tuple[bytes, str]:
    """The icon as a PNG of at most ``_ICON_PX`` a side; the original when it is not a raster image or cannot be read."""
    if "svg" in (ctype or "").lower() or len(blob) < 24_000:
        return blob, ctype or "image/png"
    try:
        from io import BytesIO

        from PIL import Image
        with Image.open(BytesIO(blob)) as image:
            image = image.convert("RGBA")
            image.thumbnail((_ICON_PX, _ICON_PX))
            out = BytesIO()
            image.save(out, format="PNG", optimize=True)
        return out.getvalue(), "image/png"
    except Exception:  # noqa: BLE001 - no imaging library, or a file it does not read: the original still shows
        return blob, ctype or "image/png"


def _answer(status: Optional[int], payload: Any) -> JSONResponse:
    if status is None:
        return JSONResponse({"ok": False, "error": "The family hub is not answering.", "code": "hub_unreachable"})
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": f"Unexpected answer from the hub (HTTP {status})."})
    if status >= 400:
        payload.setdefault("ok", False)
        payload["status"] = status
    return JSONResponse(payload)


def setup_agora_routes() -> APIRouter:
    router = APIRouter(tags=["agora"])

    @router.get("/api/agora/pulse")
    async def pulse(request: Request, since: str = "", wait_s: float = 20.0):
        require_admin(request)
        wait = max(0.0, min(float(wait_s), 20.0))
        status, payload, _ = await asyncio.to_thread(
            hub_request, "GET", "/api/agora/pulse", query={"since": since, "wait_s": str(wait)}, timeout=wait + 8.0)
        return _answer(status, payload)

    @router.get("/api/agora/where")
    async def where(request: Request):
        require_admin(request)
        return {"ok": True, "remote": bool(hub_link.location()["remote"])}

    @router.get("/api/agora/worker-activity")
    async def worker_activity(request: Request):
        require_admin(request)
        # The transcript observer covers agents across spaces. Only return the
        # aggregate needed by the pulse, never the transcripts or their paths.
        status, payload, _ = await asyncio.to_thread(
            hub_request, "GET", "/api/agora/watch", query={"limit": "1"}, timeout=5.0)
        agents = payload.get("agents") if status == 200 and isinstance(payload, dict) else None
        if not isinstance(agents, dict):
            return JSONResponse({"active_workers": None}, status_code=503)
        return {"active_workers": sum(
            isinstance(agent, dict) and agent.get("state") in ("working", "tool")
            for agent in agents.values()
        )}

    @router.get("/api/agora/icon/{app_id}")
    async def icon(app_id: str, request: Request):
        require_admin(request)
        if not _APP.match(app_id):
            return Response(status_code=404)
        cached = _icons.get(app_id)
        if cached is None and app_id in _no_icon:
            return Response(status_code=404)
        if cached is None:
            path = "/ui/hub-icon.png" if app_id == "hub" else f"/api/apps/{urllib.parse.quote(app_id, safe='')}/icon"
            status, blob, ctype = await asyncio.to_thread(hub_request, "GET", path, raw=True, timeout=10.0)
            if status != 200 or not blob:
                if status == 404 and len(_no_icon) < 500:
                    _no_icon.add(app_id)
                return Response(status_code=404)
            cached = await asyncio.to_thread(small_icon, blob, ctype or "image/png")
            if len(_icons) < 200:
                _icons[app_id] = cached
        return Response(content=cached[0], media_type=cached[1].split(";")[0], headers={"Cache-Control": "private, max-age=86400"})

    @router.get("/api/agora/file/{file_id}")
    async def attached_file(file_id: str, request: Request):
        require_admin(request)
        if not _FILE.match(file_id):
            return Response(status_code=404)
        status, blob, ctype, headers = await asyncio.to_thread(
            hub_request, "GET", "/api/agora/file/" + file_id, raw=True, timeout=60.0, with_headers=True)
        if status != 200 or blob is None:
            return Response(status_code=404 if status in (None, 404) else 502)
        return Response(content=blob, media_type=(ctype or "application/octet-stream").split(";")[0], headers={
            "Content-Disposition": headers.get("Content-Disposition") or "attachment",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})

    @router.get("/api/agora/space-icon/{space_id}")
    async def space_icon(space_id: str, request: Request):
        require_admin(request)
        if not _SPACE.match(space_id):
            return Response(status_code=404)
        status, blob, ctype = await asyncio.to_thread(
            hub_request, "GET", "/api/agora/space_icon", query={"space_id": space_id}, raw=True, timeout=10.0)
        if status != 200 or not blob or not (ctype or "").startswith("image/"):
            return Response(status_code=404)
        return Response(content=blob, media_type=ctype.split(";")[0], headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})

    @router.get("/api/agora/x/{path:path}")
    async def read(path: str, request: Request):
        require_admin(request)
        if not _PATH.match(path):
            return JSONResponse({"ok": False, "error": "unknown Agora route"}, status_code=404)
        query = {k: v for k, v in request.query_params.items()}
        status, payload, _ = await asyncio.to_thread(hub_request, "GET", "/api/agora/" + path, query=query)
        return _answer(status, payload)

    @router.post("/api/agora/x/{path:path}")
    async def write(path: str, request: Request):
        require_admin(request)
        if not _PATH.match(path):
            return JSONResponse({"ok": False, "error": "unknown Agora operation"}, status_code=404)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "error": "the body must be a JSON object"}, status_code=400)
        body.pop("_person", None)
        # A file travels inside the body, and through a tunnel when the hub is on another machine: give it time.
        status, payload, _ = await asyncio.to_thread(hub_request, "POST", "/api/agora/" + path, body=body,
                                                     timeout=180.0 if path == "file_add" else 40.0)
        return _answer(status, payload)

    return router
