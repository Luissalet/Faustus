"""Creator plugin lifecycle routes (WP32) over `src/creator/plugins.py`.

  GET  /api/creator/plugins                         List this owner's
                                                      installed plugins.
  GET  /api/creator/plugins/{id}/manifest            One plugin's current
                                                      record (manifest +
                                                      status + history tail).
  POST /api/creator/plugins/install                  discover(optional) ->
                                                      fetch -> verify ->
                                                      register.
  POST /api/creator/plugins/{id}/enable               |
  POST /api/creator/plugins/{id}/disable              |  Toggle. `enable`
                                                       |  never touches
                                                       |  approvals.
  POST /api/creator/plugins/{id}/update               Staged update, same
                                                       gate as install.
  POST /api/creator/plugins/{id}/rollback             Byte-exact restore of
                                                       the previous version.
  POST /api/creator/plugins/{id}/uninstall             Retains files for a
                                                       retention window.

Gated on `creator_enabled` (CONTRATO rule 5, default False, checked before
any store access) AND admin (`owner_is_admin_or_single_user` — the same gate
`routes/chat_routes.py` uses for server-execution tools): a plugin's
lifecycle touches what code and MCP servers this deployment runs, which is
an operator decision, not a per-conversation one. Owner is always the
authenticated session's storage owner (CONTRATO rule 3), never the body.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request

from src.auth_helpers import require_user
from src.creator import plugins as pl


def _owner(request: Request) -> str:
    from core import middleware as mw
    from src.owner_identity import effective_storage_owner
    user = require_user(request)
    owner = effective_storage_owner(user, auth_is_disabled=mw.auth_disabled())
    if not owner:
        raise HTTPException(status_code=403, detail="no storage owner for this session")
    from src.tool_security import owner_is_admin_or_single_user
    if not owner_is_admin_or_single_user(owner):
        raise HTTPException(status_code=403, detail="admin only")
    return owner


def _creator_enabled_or_404() -> None:
    from src.settings import get_setting
    if not bool(get_setting("creator_enabled", False)):
        raise HTTPException(status_code=404, detail="creator is not enabled")


async def _json_object(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return payload


def _not_found_or_owner_mismatch() -> HTTPException:
    # CONTRATO rule 3: "no es tuyo" y "no existe" responden igual.
    return HTTPException(status_code=404, detail="plugin not found")


def setup_creator_plugin_routes() -> APIRouter:
    router = APIRouter(prefix="/api/creator/plugins", tags=["creator-plugins"])

    @router.get("")
    async def list_plugins(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        return {"plugins": pl.list_plugins(owner)}

    @router.get("/{plugin_id}/manifest")
    async def get_manifest(plugin_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        try:
            record = pl.get_manifest(owner, plugin_id)
        except pl.PluginNotFound:
            raise _not_found_or_owner_mismatch()
        record = dict(record)
        record["history"] = pl.history(owner, plugin_id)[-20:]
        return record

    @router.post("/install")
    async def install_plugin(request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        body = await _json_object(request)
        source = body.get("source")
        if not isinstance(source, dict):
            raise HTTPException(400, "'source' must be an object")
        try:
            return pl.install(owner, source)
        except pl.VerificationFailed as e:
            raise HTTPException(422, {"error": "verification_failed", "reason": e.reason,
                                      "undeclared": e.undeclared})
        except pl.ManifestInvalid as e:
            raise HTTPException(422, {"error": "manifest_invalid", "reason": str(e)})
        except pl.TransportError as e:
            raise HTTPException(502, {"error": "transport_error", "reason": str(e)})
        except pl.PluginError as e:
            raise HTTPException(409, {"error": "plugin_error", "reason": str(e)})

    @router.post("/{plugin_id}/enable")
    async def enable_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        try:
            return pl.enable(owner, plugin_id)
        except pl.PluginNotFound:
            raise _not_found_or_owner_mismatch()
        except pl.PluginBlocked as e:
            raise HTTPException(403, {"error": "license_blocked", "reason": str(e)})

    @router.post("/{plugin_id}/disable")
    async def disable_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        try:
            return pl.disable(owner, plugin_id)
        except pl.PluginNotFound:
            raise _not_found_or_owner_mismatch()

    @router.post("/{plugin_id}/update")
    async def update_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        body = await _json_object(request)
        source = body.get("source")
        if not isinstance(source, dict):
            raise HTTPException(400, "'source' must be an object")
        try:
            return pl.update(owner, plugin_id, source)
        except pl.PluginNotFound:
            raise _not_found_or_owner_mismatch()
        except pl.VerificationFailed as e:
            raise HTTPException(422, {"error": "verification_failed", "reason": e.reason,
                                      "undeclared": e.undeclared})
        except pl.ManifestInvalid as e:
            raise HTTPException(422, {"error": "manifest_invalid", "reason": str(e)})
        except pl.TransportError as e:
            raise HTTPException(502, {"error": "transport_error", "reason": str(e)})
        except pl.PluginError as e:
            raise HTTPException(409, {"error": "plugin_error", "reason": str(e)})

    @router.post("/{plugin_id}/rollback")
    async def rollback_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        try:
            return pl.rollback(owner, plugin_id)
        except pl.PluginNotFound:
            raise _not_found_or_owner_mismatch()
        except pl.PluginError as e:
            raise HTTPException(409, {"error": "plugin_error", "reason": str(e)})

    @router.post("/{plugin_id}/uninstall")
    async def uninstall_plugin(plugin_id: str, request: Request) -> Dict[str, Any]:
        owner = _owner(request)
        _creator_enabled_or_404()
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        retention_days = int((body or {}).get("retention_days", 30))
        try:
            return pl.uninstall(owner, plugin_id, retention_days=retention_days)
        except pl.PluginNotFound:
            raise _not_found_or_owner_mismatch()

    return router
