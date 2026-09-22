"""plugin_runtime.py — using a plugin's app, not just talking to it.

A plugin is somebody's standalone application. Faustus connects to it; it
does not contain it, and it never owns its data. But "not owning it" is not
the same as "cannot touch it": you can ask an assistant to open a document,
and it opens the editor. Three levels, and before this module only the first
existed as something the agent could reach:

1. **Talk to it** — call its tools over the bridge. Already there, and the
   only one that worked unattended: if the app was not running, the call
   simply failed and the turn had to tell the user to go and start it.
2. **Start it** — when a plugin is needed and its app is down, start it from
   the launch profile the admin already saved, wait for the readiness check
   that profile declares, and carry on. Idempotent, and it never kills
   anything: an app the user started by hand is adopted, not restarted.
3. **Show it** — bring its window up, because "open it and show me" is a
   thing people ask for and the answer should not be a URL to click.

What this module deliberately does NOT do: invent a way to start an app that
has no launch profile. If there is none, it says so and names the plugin;
guessing a command line for somebody else's application is how you end up
running the wrong binary with the right name.

Stopping is not here either. `launch_profiles.stop` exists and is a human's
button: an agent that can stop an app it did not start is an agent that can
close the document you were writing in.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from src import plugins as plugins_mod

logger = logging.getLogger(__name__)

#: How long `ensure_running` waits for the app to answer after a launch. The
#: launch profile has its own readiness timeout; this is the ceiling on the
#: whole call so an agent turn cannot hang on an app that never comes up.
DEFAULT_WAIT_S = 30.0
POLL_S = 0.75


def _connectors() -> List[Dict[str, Any]]:
    from src import connector_sidecar

    try:
        return connector_sidecar.list_connectors(redact=True) or []
    except Exception as exc:  # noqa: BLE001 - a missing sidecar is "none configured"
        logger.debug("plugin_runtime: cannot read connectors: %s", exc)
        return []


def resolve(ref: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """Find the connector a reference means.

    `ref` may be a plugin id (``"dorian"``), a connector id, or the plugin's
    display name — an agent has all three in front of it and should not have
    to know which one this function wants.

    Returns ``{"plugin": Plugin|None, "connector": dict|None, "reason": str}``:
    a plugin with no connector is installed-but-not-connected, which is a
    different thing from an unknown id and is worth saying differently.
    """
    key = str(ref or "").strip()
    if not key:
        return {"plugin": None, "connector": None, "reason": "no plugin was named"}

    known = plugins_mod.load_plugins()
    plugin = known.get(key)
    if plugin is None:
        low = key.lower()
        for candidate in known.values():
            if candidate.name.lower() == low:
                plugin = candidate
                break

    rows = _connectors()
    if owner:
        mine = [r for r in rows if not r.get("owner") or r.get("owner") == owner]
        rows = mine or rows

    connector = None
    for row in rows:
        if row.get("id") == key or row.get("connector_id") == key:
            connector = row
            break
    if connector is None and plugin is not None:
        for row in rows:
            if row.get("preset_id") == plugin.id:
                connector = row
                break
    if connector is not None and plugin is None:
        plugin = known.get(str(connector.get("preset_id") or ""))

    if plugin is None and connector is None:
        return {"plugin": None, "connector": None,
                "reason": f"no plugin or connection called {key!r}; "
                          f"installed plugins: {', '.join(sorted(known)) or 'none'}"}
    if connector is None:
        return {"plugin": plugin, "connector": None,
                "reason": f"{plugin.name} is installed but not connected yet — "
                          f"add it from the Connectors screen and it gains a URL "
                          f"and (optionally) a way to start itself"}
    return {"plugin": plugin, "connector": connector, "reason": ""}


async def app_reachable(connector: Dict[str, Any], plugin: Optional[Any]) -> Dict[str, Any]:
    """Is the app answering right now? One real request, never cached —
    this is asked in order to decide whether to start something."""
    from src import connector_status

    app_url = str(connector.get("app_url") or (plugin.app_url_default if plugin else ""))
    if not app_url:
        return {"reachable": False, "detail": "no app URL is configured"}
    health_path = (plugin.health_path if plugin else "") or "/api/health"
    token = None
    try:
        from src.connectors import read_token_file

        token_file = str((connector.get("values") or {}).get("TOKEN_FILE") or "")
        token = read_token_file(token_file) if token_file else None
    except Exception:  # noqa: BLE001
        token = None
    try:
        return await connector_status._fetch_health(app_url, health_path, token)
    except Exception as exc:  # noqa: BLE001 - a probe never fails a turn
        return {"reachable": False, "detail": f"health check failed: {exc}"}


async def survey(*, owner: Optional[str] = None, check: bool = False) -> List[Dict[str, Any]]:
    """Every installed plugin and what can be done with it right now.

    `check` makes one health request per connected plugin. Off by default:
    listing what is installed should not touch the network.
    """
    rows = _connectors()
    if owner:
        rows = [r for r in rows if not r.get("owner") or r.get("owner") == owner] or rows
    by_preset = {str(r.get("preset_id") or ""): r for r in rows}

    out: List[Dict[str, Any]] = []
    for pid, plugin in sorted(plugins_mod.load_plugins().items()):
        connector = by_preset.get(pid)
        entry: Dict[str, Any] = {
            "plugin": pid,
            "name": plugin.name,
            "purpose": plugin.purpose,
            "capabilities": list(plugin.capabilities),
            "notes": plugin.notes,
            "source": plugin.source,
            "connected": connector is not None,
            "connector_id": (connector or {}).get("id"),
            "app_url": (connector or {}).get("app_url") or plugin.app_url_default,
            "ui_url": (connector or {}).get("ui_url"),
            "launch_profile_id": (connector or {}).get("launch_profile_id"),
        }
        entry["can_start"] = bool(entry["launch_profile_id"])
        entry["can_show"] = bool(entry["ui_url"] or entry["launch_profile_id"])
        if check and connector is not None:
            entry["app"] = await app_reachable(connector, plugin)
        out.append(entry)
    return out


async def ensure_running(ref: str, *, owner: Optional[str] = None,
                         wait_s: float = DEFAULT_WAIT_S) -> Dict[str, Any]:
    """Make sure the plugin's app is up, starting it if it is not.

    Answers one of:
      ``{"ok": True, "already_running": True}``   — it was already up
      ``{"ok": True, "started": True}``           — started and now answering
      ``{"ok": False, "reason": ...}``            — and the reason is actionable
    """
    from src import launch_profiles

    found = resolve(ref, owner=owner)
    plugin, connector = found["plugin"], found["connector"]
    if connector is None:
        return {"ok": False, "reason": found["reason"],
                "plugin": getattr(plugin, "id", None)}

    name = plugin.name if plugin else str(connector.get("preset_id") or ref)
    health = await app_reachable(connector, plugin)
    if health.get("reachable"):
        return {"ok": True, "already_running": True, "plugin": getattr(plugin, "id", None),
                "name": name, "app_url": connector.get("app_url"),
                "detail": f"{name} was already running"}

    profile_id = str(connector.get("launch_profile_id") or "")
    if not profile_id:
        return {
            "ok": False,
            "plugin": getattr(plugin, "id", None),
            "name": name,
            "reason": f"{name} is not running and has no launch profile, so there is "
                      f"nothing to start it with. Add one from the Connectors screen "
                      f"(the plugin ships a suggested command) or start the app yourself.",
        }

    try:
        launched = await launch_profiles.launch(profile_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "plugin": getattr(plugin, "id", None), "name": name,
                "reason": f"could not start {name}: {exc}"}
    if launched.get("error"):
        return {"ok": False, "plugin": getattr(plugin, "id", None), "name": name,
                "reason": f"could not start {name}: {launched['error']}"}

    # The profile's own readiness check may already have waited; poll the
    # app's health anyway, because "the process started" and "the app is
    # answering" are different claims and only the second one is useful.
    deadline = time.monotonic() + max(0.0, float(wait_s))
    while True:
        health = await app_reachable(connector, plugin)
        if health.get("reachable"):
            from src import connector_status

            connector_status.invalidate_health(str(connector.get("id") or ""))
            return {"ok": True, "started": True, "plugin": getattr(plugin, "id", None),
                    "name": name, "app_url": connector.get("app_url"),
                    "detail": f"started {name} and it is answering"}
        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "plugin": getattr(plugin, "id", None),
                "name": name,
                "started_process": True,
                "reason": f"{name} was started but did not answer within {int(wait_s)}s "
                          f"({health.get('detail') or 'no detail'}). It may still be "
                          f"coming up.",
            }
        await asyncio.sleep(POLL_S)


def show(ref: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
    """Bring the plugin's app up in front of the user."""
    from src import launch_profiles

    found = resolve(ref, owner=owner)
    plugin, connector = found["plugin"], found["connector"]
    if connector is None:
        return {"ok": False, "reason": found["reason"]}

    name = plugin.name if plugin else str(connector.get("preset_id") or ref)
    profile_id = str(connector.get("launch_profile_id") or "")
    if profile_id:
        try:
            opened = launch_profiles.open_desktop(profile_id)
        except Exception as exc:  # noqa: BLE001
            opened = {"ok": False, "error": str(exc)}
        if opened.get("ok"):
            return {"ok": True, "shown": "desktop", "name": name,
                    "detail": f"opened {name}"}
    ui_url = str(connector.get("ui_url") or "")
    if ui_url:
        return {"ok": True, "shown": "url", "name": name, "url": ui_url,
                "detail": f"{name} is at {ui_url}"}
    return {"ok": False, "name": name,
            "reason": f"{name} has no window and no UI address — its port is an API, "
                      f"not a page. Its tools are the way in."}
