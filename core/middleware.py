# src/middleware.py
# Shared middleware, decorators, and request helpers

import json
import os
import secrets
from collections.abc import Mapping

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.routing import get_route_path

from core.exceptions import http_error_class
from src.owner_identity import INTERNAL_TOOL_USER, auth_disabled


# Per-process token that lets the in-app tool layer hit admin-gated
# routes via HTTP loopback (the agent's tool calls don't carry the
# admin user's session cookie). Set once at import; tools read the
# same value from this module. Never persisted or exposed externally.
INTERNAL_TOOL_TOKEN = os.environ.get("ODYSSEUS_INTERNAL_TOKEN") or secrets.token_hex(32)
INTERNAL_TOOL_HEADER = "X-Odysseus-Internal-Token"


def get_application_route_path(scope: Mapping[str, object]) -> str:
    """Return the application-relative path used by Starlette routing.

    Uvicorn prefixes ``scope["path"]`` with a configured ASGI ``root_path``;
    Starlette removes that prefix before matching routes. Middleware policy
    must use the same path form or a deployment prefix can change which policy
    applies to an otherwise unchanged application route.
    """
    return get_route_path(scope)


def with_asgi_root_path(scope: Mapping[str, object], path: str) -> str:
    """Prefix an application path for a client-facing redirect target."""
    root_path = scope.get("root_path", "")
    if not isinstance(root_path, str) or not root_path:
        return path
    return f"{root_path.rstrip('/')}{path}"


def path_is_route_or_child(path: str, prefix: str) -> bool:
    """Return whether ``path`` is exactly ``prefix`` or below that route."""
    return path == prefix or path.startswith(prefix + "/")


def is_cors_preflight(method: str, headers) -> bool:
    """True for a genuine CORS preflight: an OPTIONS request carrying the
    Access-Control-Request-Method header. Such requests are credential-less by
    design and must reach CORSMiddleware to be answered -- gating them on auth
    401s the preflight and breaks every cross-origin browser/WebView client.
    Pure so it can be unit-tested without standing up the app."""
    return method == "OPTIONS" and "access-control-request-method" in headers


def require_admin(request: Request):
    """Raise 403 if the current user isn't an admin.
    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    """
    # In-process bypass for tool-layer loopback calls. Two paths:
    # (a) header-direct (caller set X-Odysseus-Internal-Token), or
    # (b) the auth middleware already validated the token and stamped
    #     request.state.current_user = "internal-tool".
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN):
            return
        if getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER:
            return
    except Exception:
        pass

    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_disabled():
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


def require_human(request: Request):
    """Admin, and **not the model**.

    `require_admin` deliberately accepts the in-process internal-tool token so
    the agent's loopback tool calls can reach admin routes. For most routes
    that is correct. For the handful where the whole point is that a person
    decided — granting an approval, above all — it is a hole with the shape of
    a feature: the model would be able to approve its own plan by calling the
    same endpoint the card calls.

    So this gate refuses the internal token explicitly, then defers to
    `require_admin` for everything else. It still works with auth disabled
    (the 7001 bypass), because there the browser is the human and the token is
    the only thing that distinguishes the model from them.
    """
    try:
        hdr = request.headers.get(INTERNAL_TOOL_HEADER)
        if hdr and secrets.compare_digest(hdr, INTERNAL_TOOL_TOKEN):
            raise HTTPException(403, "This action has to be taken by a person, not by a tool call")
        if getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER:
            raise HTTPException(403, "This action has to be taken by a person, not by a tool call")
    except HTTPException:
        raise
    except Exception:
        pass
    return require_admin(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add standard security headers to all responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Generate a per-request nonce for inline scripts
        nonce = secrets.token_hex(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        path = request.url.path

        # Tool render endpoints
        is_tool_render = path.startswith("/api/tools/") and path.endswith("/render")
        # Document library PDF preview endpoint
        is_document_pdf_preview = path.startswith("/api/document/") and path.endswith("/render-pdf")
        # Visual report pages are self-contained HTML — need inline scripts + external images
        is_report = path.startswith("/api/research/report/")

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"

        is_https = (
            request.url.scheme == "https"
            or request.headers.get("X-Forwarded-Proto") == "https"
        )
        if is_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        if is_report:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        elif is_tool_render:
            # Skip framing headers for tools.
            pass
        elif is_document_pdf_preview:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                "frame-ancestors 'self'"
            )
        else:
            response.headers["X-Frame-Options"] = "DENY"
            # NOTE: `style-src 'unsafe-inline'` is intentionally retained.
            # `static/index.html` and `static/login.html` ship inline <style>
            # blocks, and several JS modules build runtime `style=""` attrs.
            # Migrating to nonce-only requires templating the HTML files +
            # auditing every JS-set style attribute. Since inline styles
            # don't execute script, the residual risk is visual-only.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "font-src 'self' https://cdn.jsdelivr.net; "
                "img-src 'self' data: blob: https:; "
                "media-src 'self' blob:; "
                "connect-src 'self'; "
                "frame-src 'self'; "
                "frame-ancestors 'none'"
            )
        return await _with_error_class(response)


async def _with_error_class(response: Response) -> Response:
    """OBS-03: every HTTP error response carries `error_class` next to
    whatever it already returns -- `detail` (FastAPI/Starlette's default
    `HTTPException` handler, and every framework-level 404/405/422 no
    per-exception handler ever runs for), or the `error`/`message` shape
    app.py's four legacy `@app.exception_handler(...)` functions build by
    hand. Neither of those call sites is touched (rule 2: `app.py` is not
    owned by this lote) -- this wraps them from the outside instead, in the
    ONE middleware app.py already mounts on every response
    (`SecurityHeadersMiddleware`), so no new mount point is needed either.

    Status codes and every existing body field are left exactly as they
    were: only a new `error_class` key is added, and only when the body
    doesn't already have one. Anything that is not a JSON error body --
    success responses, and in particular any STREAMING response (chat SSE
    is 200 OK at the HTTP layer) -- is returned untouched without ever
    touching `response.body_iterator`, so a streamed reply is never
    buffered into memory here.
    """
    if response.status_code < 400:
        return response
    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        return response
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        return response
    raw = b"".join([chunk async for chunk in body_iterator])
    headers = dict(response.headers)
    headers.pop("content-length", None)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        # Not a JSON object this can extend -- put the body back unchanged
        # rather than silently dropping it.
        return Response(content=raw, status_code=response.status_code,
                         headers=headers, media_type=content_type)
    if isinstance(payload, dict) and "error_class" not in payload:
        payload["error_class"] = http_error_class(response.status_code)
    new_body = json.dumps(payload).encode("utf-8")
    return Response(content=new_body, status_code=response.status_code,
                     headers=headers, media_type="application/json")
