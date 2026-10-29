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
port of the old bridge) falls back to the preset's default or is read from
the running app. Every migration is logged and recorded on the connector
(`migrations`, newest last, never the env values themselves).

It runs when the server starts, before the MCP servers connect, when the
connector list is refreshed and on demand through
`POST /api/app-connectors/migrate`. Rows already in step are not touched:
the comparison ignores how a path is written (slashes, case).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.database import McpServer, SessionLocal
from src import connector_sidecar, connectors

logger = logging.getLogger(__name__)

#: How many migrations a connector remembers about itself.
MAX_RECORDED = 5
#: Connectors examined at once (each may probe the loopback ports).
CONCURRENCY = 8

#: A connector that could not be migrated is not tried again by a background
#: pass for this long (each try may scan the machine for its app); a startup or
#: an explicit request always tries.
BLOCKED_RETRY_S = 300.0
_blocked_at: Dict[str, float] = {}
_running: Optional["asyncio.Future"] = None

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


def _norm(value: Any) -> Any:
    """A string as the file system sees it: the same path written with
    forward or backward slashes, or another letter case, is the same path."""
    if not isinstance(value, str) or not value:
        return value
    return os.path.normcase(os.path.normpath(value))


def _same(a: Any, b: Any) -> bool:
    return a == b or _norm(a) == _norm(b)


def _blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def server_matches(resolved: Dict[str, Any], command: Any, args: Any, env: Any) -> bool:
    """True when a stored server row already is what `resolved` says.

    The environment only has to contain what the preset resolves to: extra
    variables somebody added to the row by hand do not make it stale.
    """
    if not _same(command or "", resolved["command"]):
        return False
    have_args, want_args = list(args or []), list(resolved["args"])
    if len(have_args) != len(want_args) or not all(_same(h, w) for h, w in zip(have_args, want_args)):
        return False
    have = env or {}
    return all(k in have and _same(have[k], v) for k, v in resolved["env"].items())


def _program(command: Any) -> str:
    base = os.path.basename(str(command or "").replace("\\", "/")).lower()
    return base[:-4] if base.endswith(".exe") else base


def _layout_changed(preset: connectors.ConnectorPreset, command: Any, args: List[Any]) -> bool:
    """Whether a stored server row runs a different program from the preset's
    bridge: another executable, or an argument of the preset (a script, a
    module) that the row does not have. A bridge merely installed in another
    folder is not a change of layout: the user's values still describe the
    same app. Names are compared without their folders because the template
    still holds placeholders."""
    if _program(command) != _program(preset.command):
        return True
    have = {os.path.basename(str(a).replace("\\", "/")).lower() for a in args}
    for arg in preset.args:
        if str(arg).startswith("-"):
            continue
        name = os.path.basename(str(arg).replace("\\", "/")).lower()
        if name and not name.startswith("{") and name not in have:
            return True
    return False


def _repair_values(preset: connectors.ConnectorPreset, values: Dict[str, Any],
                   *, layout_changed: bool) -> Dict[str, Any]:
    """The stored values minus the ones that cannot be right any more.

    Only when the server's layout changed, which is when the old values may
    belong to another layout: a directory that is not on disk is dropped (the
    value counts as missing and is looked up from the running app), and so are
    file paths that have a default (a token file the old app wrote: one that
    happens to exist is no proof it belongs to the new bridge). Otherwise the
    user's values are kept exactly. Blank values are never kept.
    """
    out: Dict[str, Any] = {}
    for key, value in (values or {}).items():
        if _blank(value):
            continue
        text = str(value)
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


def _default_finder_factory() -> Finder:
    """A finder that lists the listening ports once for the whole pass: every
    connector that has to look for its app would otherwise repeat the same
    scan of the machine."""
    from src import connector_discovery

    listing: Dict[str, Any] = {}
    lock = asyncio.Lock()

    async def finder(preset: connectors.ConnectorPreset, *, current_url: str = ""):
        async with lock:
            if "ports" not in listing:
                listing["ports"] = await connector_discovery._listening_ports_off_loop()
        return await connector_discovery.find_app(preset, current_url=current_url, ports=listing["ports"])

    return finder


async def _complete_values(preset: connectors.ConnectorPreset, values: Dict[str, Any],
                           *, layout_changed: bool, finder: Finder) -> Dict[str, Any]:
    """Fill what the values lack from the live app, when it is running.

    Only keys the user did not set are filled, with one exception: when the
    layout changed and the stored `APP_URL` is not this app (the old bridge
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
            if needs_url or _blank(merged.get(key)):
                merged[key] = value
        elif _blank(merged.get(key)):
            merged[key] = value
    return merged


def _read_server(factory: Callable[[], Any], server_id: Any) -> Optional[Tuple[str, List[Any], Dict[str, Any], bool]]:
    db = factory()
    try:
        server = db.query(McpServer).filter(McpServer.id == server_id).first()
        if server is None:
            return None
        return (server.command or "", _loads(server.args, []), _loads(server.env, {}),
                bool(getattr(server, "is_enabled", True)))
    finally:
        db.close()


def _write_server(factory: Callable[[], Any], server_id: Any, resolved: Dict[str, Any]) -> None:
    db = factory()
    try:
        server = db.query(McpServer).filter(McpServer.id == server_id).first()
        if server is not None:
            server.command = resolved["command"]
            server.args = json.dumps(resolved["args"])
            server.env = json.dumps(resolved["env"])
            db.commit()
    finally:
        db.close()


def _record(entry: Dict[str, Any], before: Dict[str, Any], resolved: Dict[str, Any],
            changed_values: List[str]) -> List[Dict[str, Any]]:
    history = list(entry.get("migrations") or [])
    history.append({
        "at": time.time(),
        "from": before,
        "to": {"command": resolved["command"], "args": resolved["args"]},
        "env_keys": sorted(resolved["env"]),
        "values_changed": sorted(changed_values),
    })
    return history[-MAX_RECORDED:]


def schedule_migration(manager: Any = None) -> bool:
    """Run a pass in the background unless one is already running (a refresh
    of the connector list must not wait for it). True when one was started."""
    global _running
    if _running is not None and not _running.done():
        return False
    _running = asyncio.ensure_future(migrate_connectors(manager, retry_blocked=False))
    return True


async def migrate_connectors(manager: Any = None, *, finder: Optional[Finder] = None,
                             session_factory: Optional[Callable[[], Any]] = None,
                             retry_blocked: bool = True) -> List[Dict[str, Any]]:
    """Bring every stored connector up to its preset's current definition.

    Returns one row per connector that changed or could not be migrated:
    ``{"connector_id", "preset_id", "status": "migrated"|"blocked", ...}``.
    Connectors already in step are not listed. Never raises: a connector that
    fails is reported and the rest carry on.
    """
    finder = finder or _default_finder_factory()
    factory = session_factory or SessionLocal
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with sem:
            try:
                return await _migrate_one(entry, manager, finder, factory, retry_blocked)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the others
                logger.warning("[connector-migration] %s failed: %s", entry.get("id"), exc)
                return {"connector_id": entry.get("id"), "preset_id": entry.get("preset_id"),
                        "status": "blocked", "reasons": [f"{type(exc).__name__}: {exc}"]}

    entries = await asyncio.to_thread(connector_sidecar.list_connectors, redact=False)
    rows = await asyncio.gather(*(one(e) for e in entries))
    return [r for r in rows if r]


async def _migrate_one(entry: Dict[str, Any], manager: Any, finder: Finder,
                       factory: Callable[[], Any], retry_blocked: bool = True) -> Optional[Dict[str, Any]]:
    preset = connectors.get_preset(entry.get("preset_id") or "")
    if preset is None or preset.transport != "stdio":
        return None
    if not retry_blocked and time.time() - _blocked_at.get(entry["id"], 0.0) < BLOCKED_RETRY_S:
        return None
    row = await asyncio.to_thread(_read_server, factory, entry.get("server_id"))
    if row is None:
        return None
    old_command, old_args, old_env, enabled = row

    stored = dict(entry.get("values") or {})
    layout_changed = _layout_changed(preset, old_command, old_args)
    values = _repair_values(preset, stored, layout_changed=layout_changed)
    if layout_changed or not connectors.resolve_preset_values(preset, values)["ok"]:
        values = await _complete_values(preset, values, layout_changed=layout_changed, finder=finder)
    resolved = connectors.resolve_preset_values(preset, values)
    if not resolved["ok"]:
        if not layout_changed:
            return None  # nothing the preset changed that we could act on; leave the row alone
        reasons = list(resolved.get("reasons") or []) or [
            f"missing value for {{{name}}}" for name in resolved.get("missing") or []]
        logger.warning("[connector-migration] %s (%s) still launches %s but its preset changed; "
                       "cannot migrate yet: %s", entry.get("id"), preset.id, old_command, "; ".join(reasons))
        _blocked_at[entry["id"]] = time.time()
        return {"connector_id": entry.get("id"), "preset_id": preset.id, "status": "blocked",
                "reasons": reasons}

    _blocked_at.pop(entry["id"], None)
    in_step = server_matches(resolved, old_command, old_args, old_env)
    values_changed = sorted(
        k for k in set(values) | set(stored)
        if _blank(values.get(k)) != _blank(stored.get(k))
        or (not _blank(values.get(k)) and not _same(str(values.get(k)), str(stored.get(k)))))
    if in_step:
        if values_changed:
            # The server is right; only values that cannot be used were tidied.
            await asyncio.to_thread(connector_sidecar.update_connector, entry["id"], values=values,
                                    app_url=resolved["app_url"], ui_url=resolved["ui_url"])
        return None

    before = {"command": old_command, "args": old_args}
    await asyncio.to_thread(_write_server, factory, entry["server_id"], resolved)
    was_active = False
    if manager is not None:
        try:
            was_active = manager.get_server_status(entry["server_id"]).get("status") not in (None, "disconnected")
            if was_active:
                await manager.disconnect_server(entry["server_id"])
        except Exception as exc:  # noqa: BLE001 - the new definition is saved either way
            logger.debug("[connector-migration] disconnect of %s failed: %s", entry["server_id"], exc)
    try:
        from src import connector_status
        connector_status.invalidate_health(entry["id"])
    except Exception:  # noqa: BLE001
        pass
    entry_now = await asyncio.to_thread(connector_sidecar.get_connector, entry["id"], redact=False) or entry
    await asyncio.to_thread(
        connector_sidecar.update_connector, entry["id"], values=values, app_url=resolved["app_url"],
        ui_url=resolved["ui_url"], migrations=_record(entry_now, before, resolved, values_changed))
    logger.info("[connector-migration] %s (%s) migrated: %s %s -> %s %s",
                entry["id"], preset.id, old_command, old_args, resolved["command"], resolved["args"])
    # A server that was running when it migrated comes back on the new definition.
    if was_active and enabled and hasattr(manager, "_reconnect_configured"):
        try:
            await manager._reconnect_configured(entry["server_id"], None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[connector-migration] reconnect of %s failed: %s", entry["server_id"], exc)
    return {"connector_id": entry["id"], "preset_id": preset.id, "status": "migrated",
            "from": before, "to": {"command": resolved["command"], "args": resolved["args"]},
            "values_changed": values_changed}
