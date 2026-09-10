"""Routes for the tool catalogue (TOOL-01, TOOL-03 partial): read-only
browsing over `src.tool_registry.snapshot()`, plus a dry-run that validates
arguments without ever executing anything.

Auth mirrors the neighbouring MCP routes (`routes/mcp/mcp_routes.py`):
`require_admin` on every endpoint, because the catalogue can include
admin-configured MCP server names when a live manager is attached, the same
information those routes already gate. `owner` is threaded from
`request.state.current_user` into `snapshot(owner=...)`, which narrows the
built-in half of the catalogue through the existing
`tool_security.blocked_tools_for_owner` denylist — the same authority
`agent_loop.py` already consults, not a second one invented here.
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from core.middleware import require_admin
from src.contracts.base import now_iso
from src.tool_registry import by_name, catalog_fingerprint, mcp_status_label, snapshot
from src.tool_schemas import repair_tool_arguments, validate_tool_arguments
from src.tool_utils import get_mcp_manager

logger = logging.getLogger(__name__)


def _owner_of(request: Request):
    return getattr(request.state, "current_user", None)


def _mcp_meta_for(descriptor, mcp_manager):
    """Server name/status for one MCP descriptor, or None for a built-in.

    `descriptor.executor` already carries the server id (`mcp:<server_id>`,
    set by `tool_registry._descriptor_for_mcp_tool`) — reading it back out
    here means this module never has to re-derive server ownership of a
    tool name itself."""
    if not descriptor.executor.startswith("mcp:") or mcp_manager is None:
        return None
    server_id = descriptor.executor.split(":", 1)[1]
    raw = mcp_manager.get_server_status(server_id) if server_id else {"status": "disconnected"}
    return {
        "server_id": server_id,
        "status": mcp_status_label(str(raw.get("status") or "")),
        "raw_status": raw.get("status"),
        "error": raw.get("error"),
    }


def _row_summary(descriptor, mcp_manager):
    row = {
        "name": descriptor.name,
        "version": descriptor.version,
        "description": descriptor.description,
        "effect_class": descriptor.effect_class,
        "required_scopes": list(descriptor.required_scopes),
        "timeout_ms": descriptor.timeout_ms,
        "cancellation": descriptor.cancellation,
        "idempotency": descriptor.idempotency,
        "max_output_bytes": descriptor.max_output_bytes,
        "executor": descriptor.executor,
        "mcp": _mcp_meta_for(descriptor, mcp_manager),
    }
    return row


def setup_tool_registry_routes():
    router = APIRouter(prefix="/api/tools")

    @router.get("/catalog")
    def catalog(request: Request, q: str = "", executor: str = ""):
        """The whole catalogue this owner can currently reach, optionally
        filtered by a free-text match over name/description/effect and/or an
        exact executor match ('native' | 'fence' | 'mcp:<server_id>')."""
        require_admin(request)
        mcp_manager = get_mcp_manager()
        rows = snapshot(owner=_owner_of(request), mcp_manager=mcp_manager)

        needle = q.strip().lower()
        if needle:
            rows = [
                d for d in rows
                if needle in d.name.lower()
                or needle in d.description.lower()
                or needle in d.effect_class.lower()
                or any(needle in scope.lower() for scope in d.required_scopes)
            ]
        if executor.strip():
            rows = [d for d in rows if d.executor == executor.strip()]

        return {
            "checked_at": now_iso(),
            "fingerprint": catalog_fingerprint(rows),
            "count": len(rows),
            "tools": [_row_summary(d, mcp_manager) for d in rows],
        }

    @router.get("/catalog/{name}")
    def catalog_entry(name: str, request: Request):
        """One tool's complete descriptor (including its input schema) plus
        its MCP connection state when it has one."""
        require_admin(request)
        mcp_manager = get_mcp_manager()
        rows = snapshot(owner=_owner_of(request), mcp_manager=mcp_manager)
        descriptor = by_name(rows, name)
        if descriptor is None:
            raise HTTPException(404, f"No tool named {name!r} in the catalogue")
        return {
            "checked_at": now_iso(),
            "tool": descriptor.to_mapping(),
            "mcp": _mcp_meta_for(descriptor, mcp_manager),
        }

    @router.post("/catalog/{name}/dry-run")
    async def dry_run(name: str, request: Request):
        """Validate `{"arguments": {...}}` against the tool's own schema —
        wrong type, unknown field, out-of-range enum, path-scope escape — and
        report the bounded repairs `repair_tool_arguments` would apply. Never
        calls `execute_tool_block`, a `TOOL_HANDLERS` entry, or anything else
        that would actually run the tool: this endpoint answers "would this
        call be rejected", nothing more.
        """
        require_admin(request)
        rows = snapshot(owner=_owner_of(request), mcp_manager=get_mcp_manager())
        descriptor = by_name(rows, name)
        if descriptor is None:
            raise HTTPException(404, f"No tool named {name!r} in the catalogue")

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Body must be a JSON object")
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            raise HTTPException(400, "'arguments' must be a JSON object")

        schema_available = bool(descriptor.input_schema.get("properties")) or bool(descriptor.input_schema)
        errors = validate_tool_arguments(name, arguments)
        repaired, repairs = repair_tool_arguments(name, arguments, errors)
        remaining = validate_tool_arguments(name, repaired) if repairs else errors

        return {
            "tool": name,
            "schema_available": schema_available,
            "ok": len(errors) == 0,
            "errors": [
                {"field": e.field, "kind": e.kind, "detail": e.detail, "seen": e.seen}
                for e in errors
            ],
            "repairs": repairs,
            "repaired_arguments": repaired,
            "remaining_errors": [
                {"field": e.field, "kind": e.kind, "detail": e.detail, "seen": e.seen}
                for e in remaining
            ],
        }

    return router
