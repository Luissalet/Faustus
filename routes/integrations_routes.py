"""
routes/integrations_routes.py — CONN-01: the connection center's HTTP surface.

Everything here sits ON TOP of `src.integrations` (the existing, reused
credential authority — never a second store for the same thing, rule 4) and
`src.connector_registry` (the new scope/owner/revocation layer this batch
adds, CONN-01). Gated `require_admin`, the same reading `workflows_routes.py`
already documents: an agent's in-process loopback token opens it, because
listing/declaring scopes is ordinary work — the place a person is actually
needed is the OWNER check inside each handler below, which a loopback call
satisfies the same way any other route resolves the current owner.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core import middleware
from core.middleware import require_admin
from src import connector_registry
from src.owner_identity import effective_storage_owner


async def _json_object(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _owner(request: Request) -> str:
    owner = effective_storage_owner(getattr(request.state, "current_user", None),
                                    auth_is_disabled=middleware.auth_disabled())
    return owner or ""


def setup_integrations_routes():
    router = APIRouter(prefix="/api/connectors", tags=["connectors"])

    @router.get("")
    def list_connectors(request: Request):
        """The connection center's list view: what is connected, its
        declared scopes, its status and its last sync — never the secret
        itself (`mask_integration_secret`, the existing masking this route
        reuses rather than re-inventing its own)."""
        require_admin(request)
        from src.integrations import load_integrations, mask_integration_secret
        owner = _owner(request)
        out = []
        for item in load_integrations():
            iid = item.get("id")
            info = connector_registry.effective_scopes(iid)
            if not info["unscoped"] and owner and info["owner"] != owner:
                continue  # CONN-01: isolation by owner for anything scoped through this module
            masked = mask_integration_secret(item)
            out.append({
                "id": iid, "name": masked.get("name", ""),
                "base_url": masked.get("base_url", ""),
                "enabled": masked.get("enabled", True),
                "scopes": info["scopes"], "unscoped": info["unscoped"],
                "status": info["status"], "last_synced_at": info["last_synced_at"],
            })
        return {"ok": True, "connectors": out}

    @router.get("/{integration_id}/scopes")
    def get_scopes(integration_id: str, request: Request):
        """"Ver permisos efectivos" — what this connector can do RIGHT NOW,
        not what was requested when it was connected."""
        require_admin(request)
        from src.integrations import get_integration
        if get_integration(integration_id) is None and connector_registry.get(integration_id) is None:
            raise HTTPException(status_code=404, detail="no such connector")
        return {"ok": True, **connector_registry.effective_scopes(integration_id)}

    @router.post("/{integration_id}/scopes")
    async def declare_scopes(integration_id: str, request: Request):
        """Declare (or narrow/widen) a connector's scopes. `scopes` must be a
        subset of `connector_registry.SCOPES` — anything else is a 400, not
        a silently-ignored value that would let a caller believe it holds a
        permission the registry never actually promised."""
        require_admin(request)
        payload = await _json_object(request)
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise HTTPException(status_code=400, detail="scopes must be a list of strings")
        try:
            entry = connector_registry.register(integration_id, owner=_owner(request), scopes=scopes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"ok": True, **entry}

    @router.post("/{integration_id}/revoke")
    def revoke(integration_id: str, request: Request):
        """Desconectar. Strips the credential from `src.integrations` (see
        `connector_registry.revoke`'s own docstring for why that, and not a
        flag, is what actually makes reuse impossible) and records the
        event so the connection center can still show it happened."""
        require_admin(request)
        from src.integrations import get_integration
        if get_integration(integration_id) is None:
            raise HTTPException(status_code=404, detail="no such connector")
        entry = connector_registry.revoke(integration_id)
        return {"ok": True, **entry}

    @router.post("/{integration_id}/check")
    async def check_scope(integration_id: str, request: Request):
        """Would a call with this HTTP method be allowed right now? Pure —
        no request is made. What the "probar" step in the connection center
        calls before actually dispatching, and what a caller integrating
        against this connector can use to fail fast with a clear reason
        instead of discovering the 403 from `src.integrations.execute_api_call`
        after having already built the request."""
        require_admin(request)
        payload = await _json_object(request)
        method = str(payload.get("method") or "GET")
        try:
            connector_registry.enforce_scope(integration_id, method)
        except connector_registry.ScopeError as exc:
            return {"ok": True, "allowed": False, "reason": str(exc)}
        return {"ok": True, "allowed": True}

    @router.post("/{integration_id}/call")
    async def call(integration_id: str, request: Request):
        """A minimal, scope-checked passthrough to the real credential
        store's HTTP executor (`src.integrations.execute_api_call`, reused
        verbatim — this route adds the scope gate in front of it, not a
        second way of making the request). `enforce_scope` runs BEFORE the
        import, so a call outside the declared scope never reaches the
        network at all."""
        require_admin(request)
        payload = await _json_object(request)
        method = str(payload.get("method") or "GET")
        path = str(payload.get("path") or "/")
        try:
            connector_registry.enforce_scope(integration_id, method)
        except connector_registry.ScopeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        from src.integrations import execute_api_call
        result = await execute_api_call(integration_id, method, path,
                                        params=payload.get("params"), body=payload.get("body"))
        if isinstance(result, dict) and result.get("error") and "not found" in str(result["error"]).lower():
            raise HTTPException(status_code=404, detail=result["error"])
        connector_registry.mark_synced(integration_id)
        return {"ok": True, "result": result}

    return router
