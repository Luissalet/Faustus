"""Dispatch API — local workers for an outside coordinator (src/dispatch.py).

  POST /api/dispatch                 {tasks, workspace, model?, parallel?, reviewer?,
                                      max_rounds?, timeout_s?, context?, verify?,
                                      verify_scope?, fix_rounds?, client_request_id?} → the job
                                     (header `Idempotency-Key`: a retry returns the same job)
  GET  /api/dispatch                 recent jobs (compact, no result)
  GET  /api/dispatch/config?workspace=  the resolved model/server and the verifier a
                                     job in that folder would run
  GET  /api/dispatch/guide           the coordinator guide
  GET  /api/dispatch/{id}            status + compact result (+ progress while running)
  GET  /api/dispatch/{id}/wait?timeout=N   long-poll (≤ 1800 s), then the same as GET
  GET  /api/dispatch/{id}/wait?condition=  block on a CONDITION instead of the end:
                                     done | changed | phase:<name> | event:<text> |
                                     worker:<label>:<state> → {met, condition, waited_s,
                                     state}. Resolves the moment it holds (src/dispatch.py
                                     wakes it from the job's own progress path), and a
                                     timeout answers `met: false` — not an error.
  GET  /api/dispatch/{id}/events     the board's events so far (last 400)
  GET  /api/dispatch/{id}/events?stream=1  the same events LIVE as text/event-stream:
                                     `data: {json}` per event, a `: heartbeat` comment
                                     every 15 s, a final `event: end` with the verdict.
                                     `?states=1` (non-streaming) adds what each worker's
                                     own output says about it.
  POST /api/dispatch/{id}/cancel

Without `stream=1` — and with `agent_dispatch_sse` off — `/{id}/events`
answers with exactly the JSON body it always did, byte for byte.

Robot mode (src/robot_envelope.py): `GET /api/dispatch/{id}` and
`/{id}/events` also take `?robot=1` (the standard envelope, JSON) or
`?format=toon` (the same envelope as compact TOON text) — for the
coordinating model, which then reads one shape instead of two. Both carry the
LEAN projection of the job (src/robot_projection.py): the workers and the
events as flat tables, the verdict and the verification as scalars, without
the tasks the coordinator itself sent. Without a query parameter the answers
are exactly what they always were.

Callers: an ADMIN signed in with a cookie (single-user mode counts), or an
API token with the `agents:dispatch` scope minted by an admin — the token
Fable / Claude Desktop / a script uses from outside the app. A worker runs
bash/python in any folder the caller names, as the admin: that is why a
plain user cannot dispatch (and cannot learn which host folders exist from
the workspace check). Never the model inside a chat: app_api is blocked from
this prefix (delegate_agents is its tool for that, with the chat's own gate).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from src import dispatch
from src import robot_envelope as robot
from src import robot_projection as lean
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

SCOPE = "agents:dispatch"
_MAX_WAIT_S = 1800.0
_TRUE = ("1", "true", "yes", "on")


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE


async def event_stream(request: Request, job: "dispatch.DispatchJob") -> AsyncIterator[str]:
    """The board's events as they happen, then one `end` carrying the verdict.

    The same SSE shape the rest of the app streams with (`data: {json}` per
    event, a `: heartbeat` comment so no proxy closes an idle stream, and
    `Cache-Control: no-cache` + `X-Accel-Buffering: no` on the response): see
    routes/chat_routes.py and routes/local_models_routes.py.

    It is woken by the job's own progress path (`DispatchJob._wake`), so an
    event is forwarded as it is appended and there is no poll inside. It is
    bounded by the job's ceiling plus a margin — a stream may not outlive the
    work it watches — and it unsubscribes in `finally`, so a client that walks
    away mid-stream leaves no waiter behind on the job.
    """
    sent = 0
    deadline = time.monotonic() + float(job.ceiling_s()) + dispatch.STREAM_MARGIN_S
    waiter = job.subscribe()
    try:
        while True:
            # clear BEFORE reading: an event appended during the read leaves
            # the flag set, so the next wait returns at once instead of losing it
            waiter.clear()
            rows, sent = dispatch.new_events(job, sent)
            for ev in rows:
                yield "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
            if job.status not in dispatch._LIVE:
                yield "event: end\ndata: " + json.dumps(dispatch.stream_end(job), ensure_ascii=False) + "\n\n"
                return
            left = deadline - time.monotonic()
            if left <= 0:
                yield ("event: end\ndata: "
                       + json.dumps(dispatch.stream_end(job, "the stream reached the job's ceiling"),
                                    ensure_ascii=False) + "\n\n")
                return
            if await request.is_disconnected():
                return
            try:
                await asyncio.wait_for(waiter.wait(), timeout=min(dispatch.STREAM_HEARTBEAT_S, left))
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        job.unsubscribe(waiter)


def _is_admin(owner: str) -> bool:
    try:
        from src import tool_security as ts
        return bool(ts.owner_is_admin_or_single_user(owner or None))
    except Exception:  # pragma: no cover
        return False


def _owner(request: Request) -> str:
    """An admin's cookie session, or an API token carrying the dispatch scope
    whose owner is an admin. 403 before anything about the request is looked
    at, so a non-admin learns nothing from the answer."""
    if getattr(request.state, "api_token", False):
        scopes = set(getattr(request.state, "api_token_scopes", []) or [])
        if SCOPE not in scopes:
            raise HTTPException(403, f"API token missing required scope: {SCOPE}")
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        if not _is_admin(owner):
            raise HTTPException(403, "dispatch runs workers on this machine: the token's owner must be an admin")
        return owner
    owner = require_user(request)
    if not _is_admin(owner):
        raise HTTPException(403, "dispatch runs workers on this machine: admins only")
    return owner


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "a JSON body is required")
    if not isinstance(body, dict):
        raise HTTPException(400, "the body must be a JSON object")
    return body


def setup_dispatch_routes() -> APIRouter:
    router = APIRouter(prefix="/api/dispatch", tags=["dispatch"])

    @router.post("")
    async def create(request: Request):
        owner = _owner(request)
        body = await _body(request)
        key = (request.headers.get("Idempotency-Key") or request.headers.get("X-Idempotency-Key") or "").strip()
        try:
            job = await dispatch.start(owner or None, body, idempotency_key=key or None)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return dispatch.compact(job)

    @router.get("")
    async def index(request: Request, limit: int = 50):
        owner = _owner(request)
        return {"jobs": dispatch.list_jobs(owner or None, limit=max(1, min(int(limit or 50), 200)))}

    def _get(request: Request, job_id: str) -> dispatch.DispatchJob:
        owner = _owner(request)
        job = dispatch.get(job_id)
        if job is None or not dispatch.visible_to(job, owner or None):
            raise HTTPException(404, "no such dispatch job")
        return job

    @router.get("/config")
    async def config(request: Request, workspace: str = ""):
        """Where a job would run right now: the resolved model and server,
        and — given a workspace — the verifier Faustus would run after the
        workers; so the Workers page can say both before Run."""
        owner = _owner(request)
        out: Dict[str, Any] = {"model": "", "server": "", "error": "", "verifier": None}
        try:
            url, model, _ = dispatch.resolve_route(owner or None)
            from urllib.parse import urlparse
            out.update(model=model, server=urlparse(url).netloc)
        except ValueError as e:
            out["error"] = str(e)
        ws = str(workspace or "").strip()
        if ws:
            try:
                from src.tool_execution import vet_workspace
                vetted = vet_workspace(ws)
                if vetted:
                    spec, mode = dispatch._verification_spec(vetted, "auto")
                    out["verifier"] = {"mode": mode, "label": (spec or {}).get("label") or "", "kind": (spec or {}).get("kind") or ""} \
                        if spec else {"mode": mode, "label": "", "kind": ""}
                else:
                    out["verifier"] = {"error": "workspace is not a usable directory"}
            except Exception as e:  # noqa: BLE001
                out["verifier"] = {"error": str(e)[:200]}
        return out

    @router.get("/guide")
    async def guide(request: Request):
        """How a coordinating model should use the workers — the same text
        the MCP server's `workers_guide` tool returns."""
        _owner(request)
        return {"guide": dispatch.COORDINATOR_GUIDE}

    @router.get("/{job_id}")
    async def status(request: Request, job_id: str):
        if robot.wants(request):
            return await robot.reply(
                request, lambda: lean.dispatch_status(dispatch.compact(_get(request, job_id))))
        return dispatch.compact(_get(request, job_id))

    @router.get("/{job_id}/wait")
    async def wait(request: Request, job_id: str, timeout: float = 120.0, condition: str = ""):
        """Without `condition` this is exactly what it always was: block until
        the job ends, then answer the compact job. With one, block until THAT
        condition holds and answer `{met, condition, waited_s, state}` — a
        timeout is `met: false`, never an error."""
        job = _get(request, job_id)
        try:
            t = float(timeout)
        except (TypeError, ValueError):
            t = 120.0
        t = max(0.0, min(t, _MAX_WAIT_S))
        if not str(condition or "").strip():
            await dispatch.wait(job, t)
            return dispatch.compact(job)
        try:
            return await dispatch.wait_for(job, condition=condition, timeout_s=t)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/{job_id}/events")
    async def events(request: Request, job_id: str, stream: str = "", states: str = ""):
        def payload() -> Dict[str, Any]:
            job = _get(request, job_id)
            out: Dict[str, Any] = {"id": job.id, "status": job.status, "events": list(job.events)}
            if _flag(states):
                out["states"] = dispatch.worker_states(job)
            return out
        if _flag(stream) and dispatch.sse_on() and not robot.wants(request):
            job = _get(request, job_id)
            return StreamingResponse(event_stream(request, job), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        if robot.wants(request):
            return await robot.reply(request, lambda: lean.dispatch_events(payload()))
        return payload()

    @router.post("/{job_id}/cancel")
    async def cancel(request: Request, job_id: str):
        job = _get(request, job_id)
        return {"id": job.id, "cancelled": dispatch.cancel(job), "status": job.status}

    return router
