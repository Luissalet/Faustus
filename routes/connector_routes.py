# routes/connector_routes.py
"""F1.5: `/api/app-connectors` and `/api/launch-profiles` — the Connectors screen
backend (Faustus connector plan, Phases C+D).

Reuses `McpManager` and the `McpServer` table for everything MCP already
does (principle 1): this router creates/updates/deletes `McpServer` rows
through the SAME handlers `routes/mcp/mcp_routes.py` registers at
`/api/mcp/servers*` — built once here via `setup_mcp_routes(mcp_manager)` and
looked up by (method, path) — rather than re-implementing that validation.
`/api/mcp/*` itself is untouched (rule 10: only additive).

Auth: every route uses `require_admin`, same as `routes/mcp/mcp_routes.py`
(F1.5's own text), EXCEPT the ones the contract's principle 4 singles out by
name — launch profiles (CRUD) and the two routes that can start a local
process or executable (`/connectors/{id}/launch`, `/connectors/{id}/open`).
Those use `require_human`, which explicitly refuses Faustus's own internal
agent-tool token (`core/middleware.py::require_human`): "El modelo NO
obtiene ejecución arbitraria: las herramientas del agente no pueden crear ni
editar perfiles ni pasar argv." Registering an MCP-backed connector (its argv
is the preset's own, fixed script — only directories/URLs are substituted)
stays at the same admin-only level `/api/mcp/servers` already has; only the
truly-arbitrary-executable half is narrowed further.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response

from core.database import McpServer, SessionLocal
from core.middleware import require_admin, require_human
from src import connector_discovery, connector_sidecar, connector_status, connectors, launch_profiles
from src.mcp_manager import McpManager, server_inherits_env
from routes.mcp.mcp_routes import setup_mcp_routes

logger = logging.getLogger(__name__)


def _current_owner(request: Request) -> Optional[str]:
    return getattr(request.state, "current_user", None) or None


def _server_summary(server: McpServer, manager: McpManager) -> Dict[str, Any]:
    status = manager.get_server_status(server.id)
    return {
        "id": server.id,
        "name": server.name,
        "is_enabled": bool(server.is_enabled),
        "status": status.get("status", "disconnected"),
        "tool_count": status.get("tool_count"),
    }


def _preset_dict(preset_id: Optional[str]) -> Optional[Dict[str, Any]]:
    preset = connectors.get_preset(preset_id) if preset_id else None
    if preset is None:
        return None
    for row in connectors.list_presets():
        if row["id"] == preset.id:
            return row
    return None


async def _plain_server_status(server: McpServer, manager: McpManager) -> Dict[str, Any]:
    """A `ConnectorStatus`-shaped summary for an `McpServer` with no sidecar
    entry — no preset means no app health to check, so `app` stays unknown
    and the state is derived from the adapter alone."""
    manager_status = manager.get_server_status(server.id)
    adapter = {
        "mcp_status": manager_status.get("status", "disconnected"),
        "tool_count": manager_status.get("tool_count"),
        "last_error": manager_status.get("error"),
    }
    app = {"reachable": None, "checked_at": None, "latency_ms": None, "detail": ""}
    if not server.is_enabled:
        return {"state": "disabled", "app": app, "adapter": adapter, "reasons": ["Connector is disabled"]}
    if adapter["mcp_status"] in ("connecting", "probing", "needs_auth"):
        return {"state": "connecting", "app": app, "adapter": adapter, "reasons": []}
    if adapter["mcp_status"] == "connected" and (adapter.get("tool_count") or 0) > 0:
        return {"state": "available", "app": app, "adapter": adapter, "reasons": []}
    if adapter["mcp_status"] == "connected":
        return {"state": "unknown", "app": app, "adapter": adapter, "reasons": []}
    reasons = [str(adapter["last_error"])] if adapter.get("last_error") else []
    return {"state": "error" if reasons else "unknown", "app": app, "adapter": adapter, "reasons": reasons}


def setup_connector_routes(mcp_manager: McpManager) -> APIRouter:
    # One flat router, no `include_router` nesting: this FastAPI build wraps
    # an included sub-router's routes as an opaque `_IncludedRouter` (its own
    # composed-matching optimisation), which hides the individual `APIRoute`
    # objects a direct-call test needs to look up by (method, path) — the
    # same pattern `tests/test_mcp_routes_env_mode.py` already relies on for
    # `routes/mcp/mcp_routes.py`. Every path below is written out in full
    # instead of relying on a router `prefix=`.
    router = APIRouter(tags=["connectors"])

    # Reuse the MCP server CRUD handlers instead of duplicating their
    # validation (principle 1). This second `setup_mcp_routes(...)` call
    # builds a fresh APIRouter closed over the SAME `mcp_manager` the app
    # already registered one for — it is never itself mounted on the app,
    # only used here as a lookup table for the closures inside it.
    _mcp_router = setup_mcp_routes(mcp_manager)
    _mcp_endpoints: Dict[tuple, Any] = {}
    for route in _mcp_router.routes:
        for method in getattr(route, "methods", ()) or ():
            _mcp_endpoints[(method, route.path)] = route.endpoint
    _mcp_add_server = _mcp_endpoints[("POST", "/api/mcp/servers")]
    _mcp_delete_server = _mcp_endpoints[("DELETE", "/api/mcp/servers/{server_id}")]
    _mcp_toggle_server = _mcp_endpoints[("PATCH", "/api/mcp/servers/{server_id}")]
    _mcp_list_server_tools = _mcp_endpoints[("GET", "/api/mcp/servers/{server_id}/tools")]

    async def _connector_status_for(entry: Dict[str, Any], server: Optional[McpServer],
                                     *, force_check: bool) -> Dict[str, Any]:
        preset = connectors.get_preset(entry["preset_id"])
        manager_status = mcp_manager.get_server_status(entry["server_id"]) if server else {"status": "disconnected"}
        return await connector_status.compute_status(
            preset, values=entry.get("values") or {}, is_enabled=bool(server.is_enabled) if server else False,
            connector_id=entry["id"], manager_status=manager_status, force_check=force_check,
        )

    async def _follow_app(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Jobhunter takes the first free port from 5178 up, so a stored
        APP_URL goes stale. When the configured URL does not answer, look
        for the app by its own fingerprint on the other loopback ports and,
        if it is there, move the connector: sidecar values/app_url/ui_url,
        the MCP server's env (the bridge reads JOBHUNT_URL/WH_BRIDGE_URL at
        spawn), and the cached health. Returns the (possibly updated) entry
        with `relocated_from` set when it moved."""
        preset = connectors.get_preset(entry["preset_id"])
        if preset is None:
            return entry
        resolved = connectors.resolve_preset_values(preset, entry.get("values") or {})
        if not resolved["ok"]:
            return entry
        health = await connector_status.get_health(
            entry["id"], resolved["app_url"], preset.health_path, force=True,
            token=connectors.read_token_file(resolved.get("token_file") or ""))
        if health.get("reachable"):
            return entry
        try:
            found = await connector_discovery.find_app(preset, current_url=resolved["app_url"])
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            logger.debug("[connectors] discovery failed for %s: %s", entry["id"], exc)
            found = None
        if found is None or found.url.rstrip("/") == str(resolved["app_url"]).rstrip("/"):
            return entry
        values = {**(entry.get("values") or {}), "APP_URL": found.url}
        moved = connectors.resolve_preset_values(preset, values)
        if not moved["ok"]:
            return entry
        db = SessionLocal()
        try:
            server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
            if server is not None:
                server.command = moved["command"]
                server.args = json.dumps(moved["args"])
                server.env = json.dumps(moved["env"])
                db.commit()
        finally:
            db.close()
        if mcp_manager.get_server_status(entry["server_id"]).get("status") != "disconnected":
            await mcp_manager.disconnect_server(entry["server_id"])
        connector_status.invalidate_health(entry["id"])
        updated = connector_sidecar.update_connector(
            entry["id"], values=values, app_url=moved["app_url"], ui_url=moved["ui_url"],
        ) or entry
        logger.info("[connectors] %s followed its app from %s to %s", entry["id"], resolved["app_url"], found.url)
        updated = dict(updated)
        updated["relocated_from"] = resolved["app_url"]
        return updated

    def _entry_view(entry: Dict[str, Any], server: Optional[McpServer], status: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": entry["id"],
            "preset_id": entry["preset_id"],
            "owner": entry.get("owner"),
            "values": entry.get("values") or {},
            "app_url": entry.get("app_url"),
            "ui_url": entry.get("ui_url"),
            "launch_profile_id": entry.get("launch_profile_id"),
            "preset": _preset_dict(entry["preset_id"]),
            "server": _server_summary(server, mcp_manager) if server else None,
            "status": status,
            "relocated_from": entry.get("relocated_from"),
        }

    @router.get("/api/app-connectors/discover")
    async def discover_route(request: Request):
        """Nearby apps: every loopback web app that answers, with the process
        behind it and the preset it matches — the Bluetooth-style pairing
        list. `connected` marks the ones a connector already points at."""
        require_admin(request)
        known = {}
        for entry in connector_sidecar.list_connectors(redact=True):
            known[(entry.get("preset_id"), str(entry.get("app_url") or "").rstrip("/"))] = entry["id"]
        exclude = set()
        try:
            exclude.add(int(request.url.port or 0))
        except Exception:  # noqa: BLE001
            pass
        found = await connector_discovery.discover(exclude=exclude)
        out = []
        for cand in found:
            row = cand.to_dict()
            row["connector_id"] = known.get((cand.preset_id, cand.url.rstrip("/")))
            missing = []
            if cand.preset_id:
                preset = connectors.get_preset(cand.preset_id)
                resolved = connectors.resolve_preset_values(preset, cand.values) if preset else {"ok": False, "missing": []}
                missing = list(resolved.get("missing") or []) if not resolved["ok"] else []
            row["missing"] = missing
            out.append(row)
        return {"apps": out, "scanned_at": connector_status.now_iso()}

    @router.post("/api/app-connectors/adopt")
    async def adopt_route(request: Request):
        """One-click Add for a discovered app: create the connector from the
        preset it fingerprinted as, with the values discovery could fill
        (live URL, install dir from the process cwd) plus whatever the body
        adds. Reuses the create route so validation and the 409 stay one."""
        require_admin(request)
        body = await request.json()
        port = int(body.get("port") or 0)
        if not port:
            raise HTTPException(400, "port is required")
        listing = [lp for lp in connector_discovery.listening_ports() if lp.port == port]
        found = await connector_discovery.discover(
            ports=listing or [connector_discovery.ListeningPort(port=port)], exclude=set())
        cand = next((c for c in found if c.port == port), None)
        if cand is None:
            raise HTTPException(404, f"Nothing answered on port {port}")
        preset_id = body.get("preset_id") or cand.preset_id
        if not preset_id:
            raise HTTPException(400, "This app is not a known preset; add it as an MCP server instead")
        if connectors.get_preset(preset_id) is None and cand.declares_itself:
            # Recognised only through its own faustus-plugin.json: install
            # that manifest so a preset exists, then connect as usual.
            adopted = connectors.adopt_declared_app(cand.cwd, preset_id)
            if not adopted.get("ok"):
                raise HTTPException(400, adopted.get("reason") or "could not install the app's manifest")
        values = {**cand.values, **{k: v for k, v in (body.get("values") or {}).items() if v}}
        return await _create_from_payload(request, {
            "preset_id": preset_id, "values": values,
            "name": body.get("name"), "launch_profile_id": body.get("launch_profile_id"),
        })

    @router.get("/api/app-connectors/presets")
    def get_presets(request: Request):
        require_admin(request)
        return connectors.list_presets()

    @router.get("/api/app-connectors")
    async def list_connectors_route(request: Request, check: int = 0):
        require_admin(request)
        force_check = bool(check)
        entries = connector_sidecar.list_connectors(redact=True)
        db = SessionLocal()
        try:
            servers_by_id = {s.id: s for s in db.query(McpServer).all()}
        finally:
            db.close()
        covered = {entry["server_id"] for entry in entries}

        async def _one(entry: Dict[str, Any]) -> Dict[str, Any]:
            if force_check:
                # A forced refresh is also when a moved app is followed
                # (redact=False: _follow_app needs the real values).
                raw = connector_sidecar.get_connector(entry["id"], redact=False) or entry
                followed = await _follow_app(raw)
                if followed.get("relocated_from"):
                    entry = {**(connector_sidecar.get_connector(entry["id"], redact=True) or entry),
                             "relocated_from": followed["relocated_from"]}
                    db2 = SessionLocal()
                    try:
                        srv = db2.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
                        if srv is not None:
                            servers_by_id[srv.id] = srv
                    finally:
                        db2.close()
            server = servers_by_id.get(entry["server_id"])
            status = await _connector_status_for(entry, server, force_check=force_check)
            return _entry_view(entry, server, status)

        # Every connector is checked at once. One after another, a forced
        # refresh with a few apps switched off took longer than the proxy's
        # timeout (seen live: 23 connectors, five apps off, 504 after 48 s,
        # and the Connectors screen stayed on its placeholders): each off app
        # waits out its health probe and then looks for itself on every
        # loopback port. The per-port probes are shared across them
        # (`connector_discovery.find_app`), so five off apps cost one scan.
        sem = asyncio.Semaphore(8)

        async def _bounded(entry: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                return await _one(entry)

        out = list(await asyncio.gather(*(_bounded(e) for e in entries)))
        for server_id, server in servers_by_id.items():
            if server_id in covered:
                continue
            status = await _plain_server_status(server, mcp_manager)
            out.append({
                "id": None, "preset_id": None, "owner": None, "values": {}, "app_url": None,
                "ui_url": None, "launch_profile_id": None, "preset": None,
                "server": _server_summary(server, mcp_manager), "status": status,
            })
        return out

    @router.post("/api/app-connectors")
    async def create_connector_route(request: Request):
        require_admin(request)
        body = await request.json()
        return await _create_from_payload(request, body)

    async def _create_from_payload(request: Request, body: Dict[str, Any]):
        preset_id = body.get("preset_id")
        preset = connectors.get_preset(preset_id)
        if preset is None:
            raise HTTPException(404, f"Unknown preset: {preset_id}")
        values = body.get("values") or {}
        resolved = connectors.resolve_preset_values(preset, values)
        if not resolved["ok"]:
            raise HTTPException(400, "; ".join(resolved.get("reasons") or []) or "missing required values")

        owner = _current_owner(request)
        dup = connector_sidecar.find_duplicate(preset_id, owner, resolved["app_url"])
        if dup is not None:
            raise HTTPException(409, "A connector for this preset and APP_URL already exists")

        name = body.get("name") or preset.name
        server_body = await _mcp_add_server(
            request=request, name=name, transport=preset.transport, command=resolved["command"],
            args=json.dumps(resolved["args"]), env=json.dumps(resolved["env"]), url=None,
            oauth_file=None, oauth_config=None, inherit_env=None, declared_permissions=None,
        )
        entry = connector_sidecar.create_connector(
            preset_id=preset_id, server_id=server_body["id"], owner=owner, values=values,
            app_url=resolved["app_url"], ui_url=resolved["ui_url"],
            launch_profile_id=body.get("launch_profile_id"),
        )
        db = SessionLocal()
        try:
            server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
        finally:
            db.close()
        # A real first check: the card must not open on "unknown" for an app
        # that is answering right now (adopt from the nearby list, 17-09).
        status = await _connector_status_for(entry, server, force_check=True)
        return _entry_view(connector_sidecar.get_connector(entry["id"], redact=True), server, status)

    @router.patch("/api/app-connectors/{connector_id}")
    async def update_connector_route(connector_id: str, request: Request):
        require_admin(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        body = await request.json()
        preset = connectors.get_preset(entry["preset_id"])
        updates: Dict[str, Any] = {}

        if "values" in body:
            # A client that round-trips what GET showed sends the redaction
            # marker back for secret keys; that is "leave it as it is", never
            # a new value.
            incoming = {k: v for k, v in (body.get("values") or {}).items() if v != connector_sidecar.REDACTED}
            merged_values = {**(entry.get("values") or {}), **incoming}
            resolved = connectors.resolve_preset_values(preset, merged_values) if preset else {"ok": False, "reasons": ["unknown preset"]}
            if not resolved["ok"]:
                raise HTTPException(400, "; ".join(resolved.get("reasons") or []))
            updates.update(values=merged_values, app_url=resolved["app_url"], ui_url=resolved["ui_url"])
            db = SessionLocal()
            try:
                server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
                if server is not None:
                    server.command = resolved["command"]
                    server.args = json.dumps(resolved["args"])
                    server.env = json.dumps(resolved["env"])
                    db.commit()
            finally:
                db.close()
            await mcp_manager.disconnect_server(entry["server_id"])
            connector_status.invalidate_health(connector_id)

        if "launch_profile_id" in body:
            updates["launch_profile_id"] = body["launch_profile_id"]

        if "name" in body and body["name"]:
            db = SessionLocal()
            try:
                server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
                if server is not None:
                    server.name = body["name"]
                    db.commit()
            finally:
                db.close()

        if "is_enabled" in body:
            await _mcp_toggle_server(
                server_id=entry["server_id"], request=request,
                is_enabled="true" if body["is_enabled"] else "false",
            )

        updated = connector_sidecar.update_connector(connector_id, **updates) if updates else entry
        db = SessionLocal()
        try:
            server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
        finally:
            db.close()
        status = await _connector_status_for(updated, server, force_check=False)
        return _entry_view(connector_sidecar.get_connector(connector_id, redact=True), server, status)

    @router.delete("/api/app-connectors/{connector_id}")
    async def delete_connector_route(connector_id: str, request: Request):
        require_admin(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        try:
            await _mcp_delete_server(server_id=entry["server_id"], request=request)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        connector_sidecar.delete_connector(connector_id)
        connector_status.invalidate_health(connector_id)
        return {"status": "deleted"}

    @router.post("/api/app-connectors/{connector_id}/check")
    async def check_connector_route(connector_id: str, request: Request):
        require_admin(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        entry = await _follow_app(entry)
        db = SessionLocal()
        try:
            server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
        finally:
            db.close()
        status = await _connector_status_for(entry, server, force_check=True)
        if entry.get("relocated_from"):
            status = {**status, "relocated_from": entry["relocated_from"], "app_url": entry.get("app_url")}
        return status

    @router.post("/api/app-connectors/{connector_id}/connect")
    async def connect_connector_route(connector_id: str, request: Request):
        require_admin(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        db = SessionLocal()
        try:
            server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
        finally:
            db.close()
        if server is None:
            raise HTTPException(404, "Underlying MCP server not found")
        entry = await _follow_app(entry)
        if entry.get("relocated_from"):
            db = SessionLocal()
            try:
                server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
            finally:
                db.close()
        current = mcp_manager.get_server_status(server.id)
        if current.get("status") == "connected":
            # Idempotent: connect on an already-connected server must not
            # spawn a second session.
            status = await _connector_status_for(entry, server, force_check=False)
            return {"connected": True, "status": status}
        args = json.loads(server.args) if server.args else []
        env = json.loads(server.env) if server.env else {}
        connected = await mcp_manager.connect_server(
            server_id=server.id, name=server.name, transport=server.transport, command=server.command,
            args=args, env=env, url=server.url, inherit_env=server_inherits_env(server),
        )
        status = await _connector_status_for(entry, server, force_check=False)
        return {"connected": connected, "status": status}

    @router.post("/api/app-connectors/{connector_id}/disconnect")
    async def disconnect_connector_route(connector_id: str, request: Request):
        require_admin(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        db = SessionLocal()
        try:
            server = db.query(McpServer).filter(McpServer.id == entry["server_id"]).first()
        finally:
            db.close()
        if server is not None and mcp_manager.get_server_status(server.id).get("status") != "disconnected":
            await mcp_manager.disconnect_server(server.id)
        status = await _connector_status_for(entry, server, force_check=False)
        return {"connected": False, "status": status}

    @router.get("/api/app-connectors/{connector_id}/tools")
    def connector_tools_route(connector_id: str, request: Request):
        require_admin(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        return _mcp_list_server_tools(server_id=entry["server_id"], request=request)

    @router.post("/api/app-connectors/{connector_id}/launch")
    async def launch_connector_route(connector_id: str, request: Request):
        # F1.4 / principle 4: launching a local process is a `require_human`
        # action even though the rest of this router is admin-only — the
        # agent's internal-tool token must not be able to reach it.
        require_human(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        profile_id = entry.get("launch_profile_id")
        if not profile_id:
            raise HTTPException(403, "No launch profile configured for this connector")
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(403, "The configured launch profile no longer exists")
        owner = _current_owner(request)
        if profile.get("owner") and owner and profile["owner"] != owner:
            raise HTTPException(403, "This launch profile belongs to a different user")
        client_host = request.client.host if request.client else None
        return await launch_profiles.launch(profile_id, request_client_host=client_host)

    @router.post("/api/app-connectors/{connector_id}/open")
    async def open_connector_route(connector_id: str, request: Request):
        require_human(request)
        entry = connector_sidecar.get_connector(connector_id, redact=False)
        if entry is None:
            raise HTTPException(404, "Connector not found")
        client_host = request.client.host if request.client else None
        if entry.get("ui_url"):
            note = launch_profiles.loopback_reason(entry["ui_url"], client_host)
            out = {"kind": "url", "url": entry["ui_url"]}
            if note:
                out["reasons"] = [note]
            return out
        profile_id = entry.get("launch_profile_id")
        if profile_id:
            profile = launch_profiles.get_profile(profile_id)
            if profile and profile.get("kind") == "open_exe":
                result = await launch_profiles.launch(profile_id, request_client_host=client_host)
                return {**result, "kind": "exe"}
        raise HTTPException(400, "This connector has no UI URL or open_exe launch profile")

    # ── Launch profiles: CRUD only through authenticated HTTP routes, never
    # by an agent tool (principle 4) — require_human on every verb, reads
    # included, since there is no legitimate agent use case for this list
    # either.
    @router.get("/api/launch-profiles")
    def list_launch_profiles_route(request: Request):
        require_human(request)
        return launch_profiles.list_profiles()

    # NOTE: registered before the `{profile_id}` routes below — Starlette
    # matches path routes in registration order, and "status" would
    # otherwise be swallowed as a `{profile_id}` of literally "status".
    @router.get("/api/launch-profiles/status")
    async def all_launch_profile_statuses_route(request: Request):
        # Apps wave (F1.4): a read, so `require_admin` like the rest of this
        # router's reads — only the routes that can start/stop a local
        # process stay `require_human` (principle 4).
        require_admin(request)
        return {"statuses": await launch_profiles.list_statuses()}

    @router.get("/api/launch-profiles/{profile_id}")
    def get_launch_profile_route(profile_id: str, request: Request):
        require_human(request)
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Launch profile not found")
        return profile

    @router.get("/api/launch-profiles/{profile_id}/status")
    async def launch_profile_status_route(profile_id: str, request: Request):
        require_admin(request)
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Launch profile not found")
        return await launch_profiles.status(profile_id)

    @router.post("/api/launch-profiles/{profile_id}/stop")
    async def stop_launch_profile_route(profile_id: str, request: Request):
        # F1.4 / principle 4: stopping a local process is `require_human`,
        # same reasoning as launch/open above — the agent's internal-tool
        # token must not be able to reach it.
        require_human(request)
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Launch profile not found")
        return await launch_profiles.stop(profile_id)

    @router.post("/api/launch-profiles/{profile_id}/restart")
    async def restart_launch_profile_route(profile_id: str, request: Request):
        require_human(request)
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Launch profile not found")
        return await launch_profiles.restart(profile_id)

    @router.post("/api/launch-profiles/{profile_id}/open")
    async def open_launch_profile_desktop_route(profile_id: str, request: Request):
        # Lot D: opening a desktop window is as much "start something local"
        # as launch/stop/restart above — same `require_human` gate.
        require_human(request)
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Launch profile not found")
        return launch_profiles.open_desktop(profile_id)

    @router.get("/api/launch-profiles/{profile_id}/icon")
    def launch_profile_icon_route(profile_id: str, request: Request):
        require_admin(request)
        profile = launch_profiles.get_profile(profile_id)
        if profile is None:
            raise HTTPException(404, "Launch profile not found")
        icon = launch_profiles.icon_bytes(profile_id)
        if icon is None:
            raise HTTPException(404, "No icon configured for this launch profile")
        return Response(
            content=icon["data"], media_type=icon["content_type"],
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @router.get("/api/launch-profiles/{profile_id}/log")
    def launch_profile_log_route(profile_id: str, request: Request, lines: int = 200):
        require_admin(request)
        tail = launch_profiles.log_tail(profile_id, lines=max(1, min(int(lines), 5000)))
        if tail is None:
            raise HTTPException(404, "Launch profile not found")
        return {"log": tail}

    @router.post("/api/launch-profiles")
    async def create_launch_profile_route(request: Request):
        require_human(request)
        body = await request.json()
        owner = _current_owner(request)
        try:
            return launch_profiles.create_profile(
                owner=owner, name=body.get("name") or "", kind=body.get("kind") or "",
                executable=body.get("executable") or "", argv=body.get("argv"),
                cwd=body.get("cwd") or "", env=body.get("env"),
                readiness=body.get("readiness"), url=body.get("url"),
                icon=body.get("icon"), open_url=body.get("open_url"),
                stop_cmd=body.get("stop_cmd"), description=body.get("description"),
                desktop=body.get("desktop"),
            )
        except launch_profiles.ProfileValidationError as exc:
            raise HTTPException(400, str(exc))

    @router.patch("/api/launch-profiles/{profile_id}")
    async def update_launch_profile_route(profile_id: str, request: Request):
        require_human(request)
        body = await request.json()
        try:
            updated = launch_profiles.update_profile(profile_id, **{
                k: v for k, v in body.items()
                if k in ("name", "kind", "executable", "argv", "cwd", "env", "readiness", "url",
                          "icon", "open_url", "stop_cmd", "description", "desktop")
            })
        except launch_profiles.ProfileValidationError as exc:
            raise HTTPException(400, str(exc))
        if updated is None:
            raise HTTPException(404, "Launch profile not found")
        return updated

    @router.delete("/api/launch-profiles/{profile_id}")
    def delete_launch_profile_route(profile_id: str, request: Request):
        require_human(request)
        if not launch_profiles.delete_profile(profile_id):
            raise HTTPException(404, "Launch profile not found")
        return {"status": "deleted"}

    return router
