"""src/connector_migration.py: connectors follow the preset they were made from.

A connector is created from a preset (a plugin manifest) and the MCP server
row it produced keeps the command, arguments and environment the preset
resolved to *at that moment*. When the manifest changes later (a Hoard that
used to be reached through another app's compatibility bridge becomes an app
of its own, a script moves), the stored row does not: the connector keeps
launching the old bridge, whose tools answer for an app that is not the one
the user sees. Seen live: Links kept starting the bridge of the app it had
been folded into, long after its own preset pointed at its own server.

`migrate_connectors` compares every stored connector with what its preset
resolves to today, using the values the user set, and rewrites the server row
when they differ. Values the user customised survive; a value that only made
sense for the old layout (a token file or a directory that no longer exists, a
file path from before the bridge changed) falls back to the preset's default.
Every migration is logged and recorded on the connector (`migrations`, newest
last, never the env values themselves).

It runs when the server starts, before the MCP servers connect, and on demand
through `POST /api/app-connectors/migrate`. Nothing is started or stopped
here: an already connected server is disconnected so the next connect uses
the new definition.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.database import McpServer, SessionLocal
from src import connector_sidecar, connectors

logger = logging.getLogger(__name__)

#: How many migrations a connector remembers about itself.
MAX_RECORDED = 5

#: A placeholder that names a file or directory on disk.
_PATH_SUFFIXES = ("_FILE", "_PATH", "_DIR")

Finder = Callable[..., Awaitable[Any]]


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def server_matches(preset: connectors.ConnectorPreset, resolved: Dict[str, Any],
                   command: Any, args: Any, env: Any) -> bool:
    """True when a stored server row already is what `resolved` says.

    The environment only has to contain what the preset resolves to: extra
    variables somebody added to the row by hand do not make it stale.
    """
    if (command or "") != resolved["command"]:
        return False
    if list(args or []) != list(resolved["args"]):
        return False
    have = env or {}
    return all(have.get(k) == v for k, v in resolved["env"].items())


def _layout_changed(preset: connectors.ConnectorPreset, command: str, args: List[Any]) -> bool:
    """Whether a stored server row runs a different program from the preset's
    bridge (another executable, or none of its arguments names the bridge
    script). A bridge whose script merely moved to another folder is not a
    change of layout: the values the user set still describe the same app."""
    if command != preset.command:
        return True
    script = os.path.basename(preset.args[0].replace("\\", "/")) if preset.args else ""
    return bool(script) and not any(str(a).replace("\\", "/").endswith(script) for a in args)


def _repair_values(preset: connectors.ConnectorPreset, values: Dict[str, Any],
                   *, layout_changed: bool) -> Dict[str, Any]:
    """The stored values minus the ones that cannot be right any more.

    Only when the server's command changed, which is when the old values may
    belong to another layout: a directory that is not on disk is dropped (the
    value counts as missing and is looked up from the running app), and so are
    file paths that have a default (a token file the old app wrote: one that
    happens to exist is no proof it belongs to the new bridge). Otherwise the
    user's values are kept exactly.
    """
    out: Dict[str, Any] = {}
    for key, value in (values or {}).items():
        text = str(value) if value is not None else ""
        if not text.strip():
            continue
        if layout_changed and key in preset.placeholders and key.upper().endswith(_PATH_SUFFIXES):
            upper = key.upper()
            if upper.endswith(("_FILE", "_PATH")) and key in preset.defaults:
                continue
            if upper.endswith("_DIR") and not os.path.isdir(os.path.expandvars(text)):
                continue
        out[key] = value
    return out


async def _identifies(preset: connectors.ConnectorPreset, url: str) -> bool:
    """Whether the app answering at `url` fingerprints as this preset."""
    from src import connector_discovery

    try:
        probed = await connector_discovery.probe(url)
    except Exception:  # noqa: BLE001 - an unreachable URL is just "no"
        return False
    return connector_discovery.match_preset(probed.get("health"), probed.get("title") or "") == preset.id


async def _default_finder(preset: connectors.ConnectorPreset, *, current_url: str = ""):
    from src import connector_discovery

    return await connector_discovery.find_app(preset, current_url=current_url)


async def _complete_values(preset: connectors.ConnectorPreset, values: Dict[str, Any],
                           *, layout_changed: bool, finder: Finder) -> Dict[str, Any]:
    """Fill what the values lack from the live app, when it is running.

    Only keys the user did not set are filled, with one exception: when the
    command changed and the stored `APP_URL` is not this app (the old bridge
    listened somewhere else), the URL where the app really answers wins.
    """
    resolved = connectors.resolve_preset_values(preset, values)
    needs_url = False
    if layout_changed and values.get("APP_URL") and "APP_URL" in preset.placeholders:
        needs_url = not await _identifies(preset, str(values["APP_URL"]))
    if resolved["ok"] and not needs_url:
        return values
    try:
        found = await finder(preset, current_url=str(values.get("APP_URL") or ""))
    except Exception as exc:  # noqa: BLE001 - discovery is best effort
        logger.debug("[connector-migration] discovery failed for %s: %s", preset.id, exc)
        found = None
    if found is None:
        return values
    merged = dict(values)
    for key, value in (getattr(found, "values", None) or {}).items():
        if key == "APP_URL":
            if needs_url or not merged.get(key):
                merged[key] = value
        elif not str(merged.get(key) or "").strip():
            merged[key] = value
    return merged


def _record(entry: Dict[str, Any], before: Dict[str, Any], after: Dict[str, Any],
            changed_values: List[str]) -> List[Dict[str, Any]]:
    history = list(entry.get("migrations") or [])
    history.append({
        "at": time.time(),
        "from": {"command": before["command"], "args": before["args"]},
        "to": {"command": after["command"], "args": after["args"]},
        "env_keys": sorted(after["env"]),
        "values_changed": sorted(changed_values),
    })
    return history[-MAX_RECORDED:]


async def migrate_connectors(manager: Any = None, *, finder: Optional[Finder] = None,
                             session_factory: Optional[Callable[[], Any]] = None) -> List[Dict[str, Any]]:
    """Bring every stored connector up to its preset's current definition.

    Returns one row per connector that changed or could not be migrated:
    ``{"connector_id", "preset_id", "status": "migrated"|"blocked", ...}``.
    Connectors already in step are not listed. Never raises: a connector that
    fails is reported and the rest carry on.
    """
    finder = finder or _default_finder
    factory = session_factory or SessionLocal
    report: List[Dict[str, Any]] = []
    for entry in connector_sidecar.list_connectors(redact=False):
        try:
            row = await _migrate_one(entry, manager, finder, factory)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the others
            logger.warning("[connector-migration] %s failed: %s", entry.get("id"), exc)
            row = {"connector_id": entry.get("id"), "preset_id": entry.get("preset_id"),
                   "status": "blocked", "reasons": [f"{type(exc).__name__}: {exc}"]}
        if row:
            report.append(row)
    return report


async def _migrate_one(entry: Dict[str, Any], manager: Any, finder: Finder,
                       factory: Callable[[], Any]) -> Optional[Dict[str, Any]]:
    preset = connectors.get_preset(entry.get("preset_id") or "")
    if preset is None or preset.transport != "stdio":
        return None
    db = factory()
    try:
        server = db.query(McpServer).filter(McpServer.id == entry.get("server_id")).first()
        if server is None:
            return None
        old_command = server.command or ""
        old_args = _loads(server.args, [])
        old_env = _loads(server.env, {})

        stored = dict(entry.get("values") or {})
        # Does the stored row still run this preset's bridge at all?
        layout_changed = _layout_changed(preset, old_command, old_args)

        values = _repair_values(preset, stored, layout_changed=layout_changed)
        values = await _complete_values(preset, values, layout_changed=layout_changed, finder=finder)
        resolved = connectors.resolve_preset_values(preset, values)
        if not resolved["ok"]:
            if not layout_changed:
                return None  # nothing differs that we could act on; leave the row alone
            reasons = list(resolved.get("reasons") or []) or [
                f"missing value for {{{name}}}" for name in resolved.get("missing") or []]
            logger.warning("[connector-migration] %s (%s) still launches %s but its preset changed; "
                           "cannot migrate yet: %s", entry.get("id"), preset.id, old_command, "; ".join(reasons))
            return {"connector_id": entry.get("id"), "preset_id": preset.id, "status": "blocked",
                    "reasons": reasons}

        values_changed = sorted(k for k in set(values) | set(stored) if values.get(k) != stored.get(k))
        if server_matches(preset, resolved, old_command, old_args, old_env) and not values_changed:
            return None
        if server_matches(preset, resolved, old_command, old_args, old_env):
            # The server is right; only unusable stored values were tidied.
            connector_sidecar.update_connector(entry["id"], values=values, app_url=resolved["app_url"],
                                               ui_url=resolved["ui_url"])
            return None

        before = {"command": old_command, "args": old_args}
        after = {"command": resolved["command"], "args": resolved["args"], "env": resolved["env"]}
        server.command = resolved["command"]
        server.args = json.dumps(resolved["args"])
        server.env = json.dumps(resolved["env"])
        db.commit()
    finally:
        db.close()

    if manager is not None:
        try:
            if manager.get_server_status(entry["server_id"]).get("status") not in (None, "disconnected"):
                await manager.disconnect_server(entry["server_id"])
        except Exception as exc:  # noqa: BLE001 - the new definition is saved either way
            logger.debug("[connector-migration] disconnect of %s failed: %s", entry["server_id"], exc)
    try:
        from src import connector_status
        connector_status.invalidate_health(entry["id"])
    except Exception:  # noqa: BLE001
        pass
    entry_now = connector_sidecar.get_connector(entry["id"], redact=False) or entry
    connector_sidecar.update_connector(
        entry["id"], values=values, app_url=resolved["app_url"], ui_url=resolved["ui_url"],
        migrations=_record(entry_now, before, after, values_changed))
    logger.info("[connector-migration] %s (%s) migrated: %s %s -> %s %s",
                entry["id"], preset.id, old_command, old_args, after["command"], after["args"])
    return {"connector_id": entry["id"], "preset_id": preset.id, "status": "migrated",
            "from": before, "to": {"command": after["command"], "args": after["args"]},
            "values_changed": values_changed}
