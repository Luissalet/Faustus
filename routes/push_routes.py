"""routes/push_routes.py — `/api/push/*`, Web Push subscription management
(lot P-A). See `src/push.py` for the RFC 8291/8292 implementation and
`docs/api/mobile.md` for the endpoint contract and the payload shape the
service worker's `push` event handler receives.

Auth follows the same pattern `routes/mobile_routes.py::_mobile_owner`
established: `require_admin` alone 403s a companion-paired phone's bearer
token (it always resolves to the sandboxed `"api"` pseudo-user, never a
real admin — see that module's docstring), so a bearer caller is trusted
directly here too and only a cookie caller goes through the real
`require_admin`.

Importing this module registers `src/push.py`'s bus sink
(`notifications.register_sink`) exactly once — `app.py` is off limits for
this lot, so this import (which `app.py` already does, to mount the router)
is the natural place for it instead of a startup event this module can't
add.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.middleware import require_admin
from src import push
from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)


def _push_owner(request: Request) -> str:
    """Who this `/api/push/*` request acts as. Same shape as
    `routes/mobile_routes.py::_mobile_owner` — a bearer token from
    companion pairing is trusted directly (already scope-checked by
    `AuthMiddleware`/`core/authz.py`), a cookie caller goes through the
    real `require_admin`."""
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


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionJSON(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys
    expirationTime: object = None


class SubscribeBody(BaseModel):
    subscription: PushSubscriptionJSON
    device_name: str = Field(default="", max_length=200)


class UnsubscribeBody(BaseModel):
    endpoint: str


def setup_push_routes() -> APIRouter:
    router = APIRouter(prefix="/api/push", tags=["push"])

    @router.get("/vapid-key")
    def get_vapid_key(request: Request):
        _push_owner(request)
        return {"key": push.vapid_public_key()}

    @router.post("/subscribe")
    def subscribe(body: SubscribeBody, request: Request):
        owner = _push_owner(request)
        try:
            row = push.subscribe(
                owner,
                body.subscription.model_dump(),
                device_name=body.device_name,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True, "subscription": row}

    @router.post("/unsubscribe")
    def unsubscribe(body: UnsubscribeBody, request: Request):
        owner = _push_owner(request)
        removed = push.unsubscribe(owner, body.endpoint)
        return {"ok": True, "removed": removed}

    @router.get("/subscriptions")
    def subscriptions(request: Request):
        owner = _push_owner(request)
        return {"subscriptions": push.list_subscriptions(owner)}

    @router.post("/test")
    def test(request: Request):
        owner = _push_owner(request)
        payload = {
            "title": "Faustus",
            "body": "Faustus está conectado",
            "url": "/",
            "kind": "test",
            "id": None,
        }
        results = push.broadcast(owner, payload, urgency="normal")
        return {"ok": True, "results": results}

    return router


# Registered at import time (see module docstring): `app.py` imports this
# module once to mount the router, which is the one guaranteed "the app is
# starting up" hook available to a lot that cannot touch app.py itself.
# `push.start()` itself is idempotent, so this is safe even if some future
# caller imports the module more than once (e.g. a test re-importing it).
push.start()
