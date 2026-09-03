"""Agent gate route — ``POST /api/agent-gate/{run_token}`` (FAUSTUS).

The loopback endpoint a foreign agent's PreToolUse hook posts to. It carries
no policy of its own: it unpacks the request and hands it to
:func:`src.agent_gate.handle_hook`, which is the same function
:class:`src.agent_gate.GateServer` calls. One policy, two transports — a
second door into a guard is how a guard grows a second, weaker answer.

Three things about this route, in the order they matter:

* **The credential is in the path and it is per run.** ``run_token`` is minted
  at spawn by :func:`src.agent_gate.open_run`, has ~256 bits of entropy, is
  never persisted, and stops working the moment the run ends. It is
  deliberately NOT the app's internal-tool token: a foreign process holding one
  of those could reach half the admin surface, and the point of this endpoint
  is that the process holding its credential can do exactly one thing with it.
  Unknown, expired and finished all answer 404, so probing cannot tell them
  apart.
* **A model must never call this.** ``include_in_schema=False`` keeps it out of
  the OpenAPI surface the model-facing bridge lists, and the handler refuses
  any request carrying the internal-tool header that bridge sends — belt and
  braces, because the two failures are independent. It should ALSO be listed in
  ``_APP_API_BLOCKLIST_METHOD_PATH`` in src/tools/system.py; that file is not
  part of this work, and the exact line is in this change's report.
* **It is loopback-only**, checked on the request's own client address rather
  than on a header a caller could set.

Registering this router is :func:`install`, which is idempotent. Note that on
an instance with ``AUTH_ENABLED`` the app's auth middleware will 401 this path
before the handler sees it unless it is also auth-exempt — which is why
src/external_worker.py uses the standalone per-run listener instead, and why
this route is the alternative rather than the only door.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def setup_agent_gate_routes() -> APIRouter:
    router = APIRouter(tags=["agent-gate"])

    @router.post("/api/agent-gate/{run_token}", include_in_schema=False)
    async def agent_gate(run_token: str, request: Request) -> JSONResponse:
        """Judge one foreign tool call. Answers Claude Code's hook shape."""
        from src import agent_gate as gate
        try:
            payload: Any = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is judged, not crashed
            payload = {}
        internal = False
        try:
            from core.middleware import INTERNAL_TOOL_HEADER
            internal = bool(request.headers.get(INTERNAL_TOOL_HEADER))
        except Exception:  # noqa: BLE001
            internal = bool(request.headers.get("X-Odysseus-Internal-Token"))
        # A proxy header means the connection came through something, so the
        # loopback client address is the proxy's and proves nothing about who
        # is really calling.
        for header in ("x-forwarded-for", "x-real-ip", "cf-connecting-ip", "forwarded"):
            if request.headers.get(header):
                return JSONResponse(
                    status_code=403,
                    content={"error": "the agent gate answers direct loopback callers only"},
                )
        host = request.client.host if request.client else ""
        status, body = gate.handle_hook(
            run_token, payload, client_host=host, internal_token_seen=internal,
        )
        return JSONResponse(status_code=status, content=body)

    return router


_INSTALLED: Dict[int, bool] = {}


def install(app: Any) -> bool:
    """Register the router on `app` once. True when it is now installed.

    Idempotent by app identity so a second gated run does not add a second
    copy of the route to the table.
    """
    try:
        key = id(app)
        if _INSTALLED.get(key):
            return True
        app.include_router(setup_agent_gate_routes())
        _INSTALLED[key] = True
        return True
    except Exception as e:  # noqa: BLE001 - a run that cannot install the route
        # falls back to the standalone listener; it never runs ungated in
        # silence (src/external_worker.py decides that, and says so).
        logger.warning("agent gate route could not be installed: %r", e)
        return False


__all__ = ["install", "setup_agent_gate_routes"]
