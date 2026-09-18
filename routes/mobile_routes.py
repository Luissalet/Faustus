"""routes/mobile_routes.py — `/api/mobile/*`, the native Android app's server
surface (lot M-A). Everything here is additive: no existing route's shape or
auth changes, and every read reuses the same DB tables/session_manager the
desktop Studio already reads — this file adds a compact, phone-shaped view
over them plus the notification bus in `src/notifications.py`.

## Auth — what the contract assumed, and what is actually true

The mobile contract (`mobile_contract.md`) says routes here are "all
`require_admin` — the bearer token from pairing already satisfies it". That
is false as written, and the fix lives in `_mobile_owner` below:

`require_admin` (`core/middleware.py`) checks `auth_mgr.is_admin(user)` where
`user = request.state.current_user`. For a bearer `ody_` token, `AuthMiddleware`
(app.py) always stamps `current_user = "api"` (the sandboxed pseudo-user —
see `src/auth_helpers.effective_user`'s own docstring) — never a real
username — so `is_admin("api")` is always False and a plain `require_admin`
call 403s every companion-paired phone. `routes/approvals_routes.py::
_read_owner` already works around exactly this for its own bearer-token
reads; `_mobile_owner` here is the same pattern generalized to this whole
router: a bearer token is trusted directly (its scope was already checked by
`AuthMiddleware`/`core/authz.py` before the request reached this module — see
that file's `/api/mobile` rule), and only a cookie caller still goes through
the real `require_admin`.

`POST /api/chat_stream` has the same shape of problem, for a different
reason: the deny-by-default token matrix (`core/authz.py`) requires the
`sessions` scope for that route, but the companion pairing token is minted
with `scope="chat"` only (`companion/pairing.py:COMPANION_SCOPE`) — not a
CSRF issue, a scope one. `POST /session/{sid}/send` below is the shim the
contract anticipated for this case: it loopbacks to the real
`/api/chat_stream` using the internal-tool token with owner impersonation
(the same mechanism `src/builtin_actions.py`'s Cookbook actions already use
for admin-gated loopback calls), so the streaming logic is never duplicated.

See `docs/api/mobile.md` for the endpoint table and the WebSocket protocol.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import bcrypt
import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_TOKEN,
    require_admin,
)
from src import notifications as notifications_bus
from src.auth_helpers import effective_user
from src.constants import internal_api_base

logger = logging.getLogger(__name__)

#: How often the WS handler sends its own keepalive `{"type":"ping"}` frame.
#: Android's default idle-socket timeouts on mobile networks are commonly
#: well under a minute; 25s keeps well clear of that without being chatty.
WS_PING_INTERVAL_S = 25.0

#: Plain text only, capped (contract: "no images/blobs" on the messages
#: read). A message body beyond this is still real content on the desktop —
#: just not something worth pushing to a phone screen in one bubble.
MESSAGE_PREVIEW_CHARS = 4000


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #

def _mobile_owner(request: Request) -> str:
    """Who this `/api/mobile/*` request acts as. See the module docstring
    for why this is not a plain `require_admin(request)` call."""
    if getattr(request.state, "api_token", False):
        owner = getattr(request.state, "api_token_owner", None)
        if not owner:
            raise HTTPException(403, "API token has no owner")
        return owner
    require_admin(request)
    user = effective_user(request)
    if not user:
        raise HTTPException(403, "Admin only")
    return user


def _verify_bearer_token(raw_token: str) -> Optional[str]:
    """Standalone bearer-token check for the WebSocket handshake.

    `AuthMiddleware` is a Starlette `BaseHTTPMiddleware`, and Starlette never
    runs HTTP middleware against a `websocket` ASGI scope — confirmed against
    a live TestClient WS connection while writing this lot (a request with no
    valid cookie/bearer sails straight into the handler with `request.state`
    unset). So `GET /api/mobile/ws` cannot rely on the middleware having done
    anything and must authenticate for itself.

    This mirrors the relevant slice of that middleware's own bearer check
    (prefix lookup, then bcrypt) directly against the DB, since the
    middleware's token cache is a private module-level closure in `app.py`
    with nothing exported to reuse. Returns the token's owner, or None.
    """
    from core.database import ApiToken, SessionLocal

    raw_token = (raw_token or "").strip()
    if not raw_token.startswith("ody_") or not (12 <= len(raw_token) <= 100):
        return None
    prefix = raw_token[:8]
    db = SessionLocal()
    try:
        rows = (
            db.query(ApiToken)
            .filter(ApiToken.is_active == True, ApiToken.token_prefix == prefix)  # noqa: E712
            .all()
        )
        for row in rows:
            try:
                if bcrypt.checkpw(raw_token.encode(), row.token_hash.encode()):
                    return row.owner or None
            except (ValueError, TypeError):
                continue
    finally:
        db.close()
    return None


def _bearer_from_ws(websocket: WebSocket) -> str:
    """`?token=` or an `Authorization: Bearer` header — the contract asks for
    both because some Android WS client stacks make setting a handshake
    header awkward, but the header is honored too when it's there."""
    token = (websocket.query_params.get("token") or "").strip()
    if token:
        return token
    auth = websocket.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


# --------------------------------------------------------------------------- #
# Route setup
# --------------------------------------------------------------------------- #

def setup_mobile_routes() -> APIRouter:
    router = APIRouter(prefix="/api/mobile", tags=["mobile"])

    # ------------------------------------------------------------------ #
    # GET /api/mobile/bootstrap
    # ------------------------------------------------------------------ #
    @router.get("/bootstrap")
    def bootstrap(request: Request) -> Dict[str, Any]:
        owner = _mobile_owner(request)
        from core.constants import APP_VERSION
        from core.database import SessionLocal
        from core.database import Session as DbSession
        from src import agent_runs, approval_store
        from src.auth_helpers import owner_filter

        db = SessionLocal()
        try:
            sessions_count = (
                owner_filter(db.query(DbSession.id), DbSession, owner)
                .filter(DbSession.archived == False)  # noqa: E712
                .count()
            )
            active_ids = set(agent_runs.active_session_ids())
            active_turns = 0
            if active_ids:
                active_turns = (
                    owner_filter(db.query(DbSession.id), DbSession, owner)
                    .filter(DbSession.id.in_(active_ids))
                    .count()
                )
        finally:
            db.close()

        pending_approvals = len(approval_store.pending(owner=owner, limit=200))

        return {
            "version": APP_VERSION,
            "server_name": "Faustus",
            "me": owner,
            "capabilities": {
                "chat": True, "calendar": True, "notes": True,
                "approvals": True, "whatsapp": True, "cards": True,
            },
            "counts": {
                "sessions": sessions_count,
                "pending_approvals": pending_approvals,
                "active_turns": active_turns,
            },
        }

    # ------------------------------------------------------------------ #
    # GET /api/mobile/notifications
    # ------------------------------------------------------------------ #
    @router.get("/notifications")
    def get_notifications(request: Request, since_id: int = 0, limit: int = 50) -> Dict[str, Any]:
        owner = _mobile_owner(request)
        rows, last_id = notifications_bus.list_events(owner=owner, since_id=since_id, limit=limit)
        return {"items": rows, "last_id": last_id}

    # ------------------------------------------------------------------ #
    # GET /api/mobile/sessions
    # ------------------------------------------------------------------ #
    @router.get("/sessions")
    def list_sessions(request: Request, limit: int = 30) -> Dict[str, Any]:
        owner = _mobile_owner(request)
        limit = max(1, min(int(limit or 30), 100))
        from core.database import ChatMessage as DbChatMessage
        from core.database import Session as DbSession
        from core.database import SessionLocal
        from src import agent_runs
        from src.auth_helpers import owner_filter

        db = SessionLocal()
        try:
            rows = (
                owner_filter(db.query(DbSession), DbSession, owner)
                .filter(DbSession.archived == False)  # noqa: E712
                .order_by(DbSession.updated_at.desc())
                .limit(limit)
                .all()
            )
            running_ids = set(agent_runs.active_session_ids())
            out: List[Dict[str, Any]] = []
            for row in rows:
                last_msg = (
                    db.query(DbChatMessage.content)
                    .filter(DbChatMessage.session_id == row.id)
                    .order_by(DbChatMessage.timestamp.desc())
                    .first()
                )
                preview = (last_msg[0] if last_msg else "") or ""
                preview = preview.strip().replace("\n", " ")[:200]
                out.append({
                    "id": row.id,
                    "name": row.name,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    "model": row.model,
                    "mode": row.mode,
                    "running": row.id in running_ids,
                    "last_preview": preview,
                })
        finally:
            db.close()
        return {"sessions": out}

    # ------------------------------------------------------------------ #
    # GET /api/mobile/session/{sid}/messages
    # ------------------------------------------------------------------ #
    @router.get("/session/{sid}/messages")
    def get_messages(request: Request, sid: str, limit: int = 60, before: str = "") -> Dict[str, Any]:
        owner = _mobile_owner(request)
        limit = max(1, min(int(limit or 60), 200))
        from core.database import ChatMessage as DbChatMessage
        from core.database import Session as DbSession
        from core.database import SessionLocal

        db = SessionLocal()
        try:
            db_session = db.query(DbSession).filter(DbSession.id == sid).first()
            if db_session is None or (db_session.owner and db_session.owner != owner):
                raise HTTPException(404, f"Session '{sid}' not found")
            q = db.query(DbChatMessage).filter(DbChatMessage.session_id == sid)
            before = (before or "").strip()
            if before:
                try:
                    cursor = datetime.fromisoformat(before)
                    q = q.filter(DbChatMessage.timestamp < cursor)
                except ValueError:
                    raise HTTPException(400, "before must be an ISO-8601 timestamp")
            rows = q.order_by(DbChatMessage.timestamp.desc()).limit(limit).all()
            rows = list(reversed(rows))  # newest last, per contract

            messages: List[Dict[str, Any]] = []
            for m in rows:
                meta: Dict[str, Any] = {}
                if m.meta_data:
                    try:
                        meta = json.loads(m.meta_data) or {}
                    except (ValueError, TypeError):
                        meta = {}
                entry: Dict[str, Any] = {
                    "id": m.id,
                    "role": m.role,
                    "content": (m.content or "")[:MESSAGE_PREVIEW_CHARS],
                    "created_at": m.timestamp.isoformat() if m.timestamp else None,
                }
                tool_events = meta.get("tool_events") if isinstance(meta, dict) else None
                if tool_events:
                    names = [str(t.get("tool") or t.get("name") or "").strip()
                             for t in tool_events if isinstance(t, dict)]
                    names = [n for n in names if n]
                    if names:
                        entry["tool_calls_summary"] = "used: " + ", ".join(dict.fromkeys(names))
                messages.append(entry)
        finally:
            db.close()
        return {"messages": messages}

    # ------------------------------------------------------------------ #
    # POST /api/mobile/session/{sid}/send — the chat_stream shim.
    #
    # Not needed for a token holding the `sessions` scope, but the
    # companion pairing token holds only `chat` (see module docstring), so
    # a direct `POST /api/chat_stream` bearer call 403s on scope before it
    # ever reaches that route. This loopbacks to the real endpoint over
    # HTTP using the internal-tool token with owner impersonation — the
    # same mechanism already used elsewhere for admin-gated loopback calls
    # (`src/builtin_actions.py`'s Cookbook actions) — and streams the exact
    # same SSE bytes back, so the ~2000 lines of chat_stream logic are
    # never duplicated here.
    # ------------------------------------------------------------------ #
    @router.post("/session/{sid}/send")
    async def send_message(request: Request, sid: str) -> StreamingResponse:
        owner = _mobile_owner(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object")
        body = dict(body)
        body["session"] = sid

        base = internal_api_base()
        headers = {
            INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN,
            "X-Odysseus-Owner": owner,
            "Content-Type": "application/json",
        }

        client = httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(10.0, read=300.0))

        async def _relay():
            try:
                async with client.stream(
                    "POST", "/api/chat_stream", json=body, headers=headers,
                ) as upstream:
                    if upstream.status_code >= 400:
                        detail = await upstream.aread()
                        yield (
                            "event: error\n"
                            f"data: {json.dumps({'error': detail.decode(errors='replace')[:500]})}\n\n"
                        ).encode()
                        return
                    async for chunk in upstream.aiter_raw():
                        if chunk:
                            yield chunk
            except httpx.HTTPError as exc:
                yield (
                    "event: error\n"
                    f"data: {json.dumps({'error': str(exc)})}\n\n"
                ).encode()
            finally:
                await client.aclose()

        return StreamingResponse(_relay(), media_type="text/event-stream")

    # ------------------------------------------------------------------ #
    # WS /api/mobile/ws — event push.
    # ------------------------------------------------------------------ #
    @router.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        raw_token = _bearer_from_ws(websocket)
        owner = _verify_bearer_token(raw_token) if raw_token else None
        if not owner:
            # Close with an application-specific code (per FastAPI/Starlette
            # convention: 4000–4999 is the app's own range) rather than
            # accepting and then dropping the connection — a client can
            # branch on this code to show "pairing expired" instead of
            # retrying forever.
            await websocket.close(code=4401)
            return

        await websocket.accept()
        # Subscribe BEFORE reporting `last_id`, so there is no gap between
        # "what the client should call GET /notifications?since_id= for"
        # and "what this socket starts pushing live" — a subscribe after
        # reading the ring's tail could miss an event emitted in between.
        queue = notifications_bus.subscribe(owner)
        last_id = notifications_bus.latest_id()
        await websocket.send_json({"type": "hello", "last_id": last_id})

        # Three independent loops, run concurrently rather than as one
        # cancel-and-recreate-per-iteration state machine: forwarding a
        # queued event, sending the periodic keepalive, and reading whatever
        # the client sends (`ack`/`pong` — both no-ops; the ring is the
        # source of truth, so there is nothing to advance on an ack) never
        # need to block on each other, and this way a slow/silent client
        # still gets its events and pings on schedule.
        async def _pump_events():
            while True:
                event = await queue.get()
                await websocket.send_json({"type": "event", "event": event})

        async def _pump_pings():
            while True:
                await asyncio.sleep(WS_PING_INTERVAL_S)
                await websocket.send_json({"type": "ping"})

        async def _read_client():
            while True:
                # Content is deliberately unexamined beyond "is it JSON" —
                # `ack`/`pong` are the only messages the protocol defines
                # and both are no-ops; ignoring anything else keeps this
                # forward-compatible with a client field this server
                # doesn't know about yet.
                await websocket.receive_json()

        tasks = [
            asyncio.ensure_future(_pump_events()),
            asyncio.ensure_future(_pump_pings()),
            asyncio.ensure_future(_read_client()),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.debug("mobile ws handler ended abnormally", exc_info=True)
        finally:
            for task in tasks:
                task.cancel()
            notifications_bus.unsubscribe(owner, queue)

    return router
