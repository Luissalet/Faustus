"""Resolve family connectors through the PC Hub when Faustus lives elsewhere.

Stored commands, values and browser URLs retain their PC meaning. The Hub
recognises family app ids from its installed manifests and supplies the runtime
transport. A non-family preset receives 404 and follows its ordinary path.
"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from src import connector_sidecar, hoard_hub, hub_link


async def describe(preset_id, *, check=False):
    linked = hub_link.link()
    if not linked or not preset_id:
        return None
    path = "/api/mcp/apps/" + quote(str(preset_id), safe="")
    if check:
        path += "?check=1"
    code, body, _ = await asyncio.to_thread(hoard_hub._call, "GET", path, timeout=5, where=linked)
    if code == 404:
        return None
    if code != 200 or not isinstance(body, dict) or not body.get("mcp_path"):
        raise RuntimeError("PC Hub MCP relay unavailable" + (": " + str(body.get("error")) if isinstance(body, dict) and body.get("error") else ""))
    return {**body, "url": linked["url"] + body["mcp_path"], "token": hub_link.token(linked)}


async def for_server(server_id, command=None, args=None):
    if not hub_link.link():
        return None
    entry = await asyncio.to_thread(connector_sidecar.get_connector_for_server, server_id)
    if entry:
        return await describe(entry.get("preset_id"))
    linked = hub_link.link()
    code, body, _ = await asyncio.to_thread(hoard_hub._call, "POST", "/api/mcp/resolve",
        {"command": command, "args": args or []}, timeout=5, where=linked)
    if code == 200 and isinstance(body, dict) and isinstance(body.get("app"), dict):
        app = body["app"]
        return {**app, "url": linked["url"] + app["mcp_path"], "token": hub_link.token(linked)}
    return None


async def start(entry):
    app = await describe(entry.get("preset_id"))
    if app is None:
        return None
    code, result = await asyncio.to_thread(hub_link.post, "/api/apps/" + quote(app["id"], safe="") + "/start", {"wait": True}, timeout=120)
    if code != 200 or not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(str((result or {}).get("error") or "PC Hub could not start the app"))
    return result
