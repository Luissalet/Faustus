"""OBS-01 — the HTTP door onto `src.agent_runs.trace_for_call`.

`trace_for_call` itself (commit a674903) already reconstructs everything one
tool call produced — its events, its artifact manifest row, its
command_guard receipt — keyed by nothing but the `call_id` the observability
lots already stamp on all three. What did not exist yet was a route: a way
to ask for that reconstruction without importing the module directly (a
debugging UI, a support script, `curl`).

Auth mirrors the rest of chat's session-scoped debugging surface
(`routes/chat_routes.py`'s `/api/chat/activity`, `_verify_session_owner`):
a caller who names `session_id` may only trace within a session they own: no
different than reading that session's own event stream. Omitting
`session_id` searches every session on the instance — the same
cross-session reach `trace_for_call(call_id)` has always had — and is
therefore admin-gated, the same rule `routes/contracts_routes.py` already
applies to its own instance-wide read endpoints.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from core.middleware import require_admin
from routes.session_routes import _verify_session_owner


def setup_observability_routes():
    router = APIRouter(prefix="/api/observability")

    @router.get("/trace/{call_id}")
    async def trace_call(call_id: str, request: Request, session_id: str = ""):
        """Everything produced under one `call_id`: its events (already
        carrying `trace_id`/`step_id`), its artifact manifest row(s) if it
        made one, and its command_guard receipt if it ran a guarded
        command — joined without grepping three stores by hand (OBS-01's
        acceptance: "se pueden enlazar sin buscar manualmente por texto")."""
        session_id = (session_id or "").strip()
        if session_id:
            _verify_session_owner(request, session_id)
        else:
            require_admin(request)

        from src import agent_runs
        return agent_runs.trace_for_call(call_id, session_id=session_id or None)

    return router
