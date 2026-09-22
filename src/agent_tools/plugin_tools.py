"""plugin_tools.py — the agent's side of "use one of my applications".

Two tools, split by what they cost rather than by subject:

* ``plugins_list`` reads. What is installed, what is connected, what each one
  lends Faustus. No approval card, because looking at a list is not an act.
* ``plugin_app`` starts an application on this machine and can bring its
  window up. That is an act, and it is declared as one.

Neither can stop an app, and neither can start one that has no launch
profile — see `src/plugin_runtime.py` for why both of those are refusals
rather than gaps.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _owner(ctx: Optional[Dict[str, Any]]) -> Optional[str]:
    return (ctx or {}).get("owner") or None


def _args(content: Any) -> Dict[str, Any]:
    """Tool content as a dict, whether it arrived as JSON text or already
    parsed. A bare string is taken as the plugin reference, which is what a
    model writes when it is in a hurry."""
    if isinstance(content, dict):
        return dict(content)
    text = str(content or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"plugin": text}
    return dict(parsed) if isinstance(parsed, dict) else {"plugin": str(parsed)}


class PluginsListTool:
    """What applications this Faustus can use, and whether they are up."""

    async def execute(self, content: Any, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src import plugin_runtime

        args = _args(content)
        check = bool(args.get("check"))
        try:
            rows = await plugin_runtime.survey(owner=_owner(ctx), check=check)
        except Exception as exc:  # noqa: BLE001
            logger.warning("plugins_list failed: %s", exc)
            return {"error": f"could not read the installed plugins: {exc}"}

        if not rows:
            return {"output": "No plugins are installed.", "plugins": []}

        lines = []
        for row in rows:
            bits = [f"{row['name']} ({row['plugin']})"]
            if row["capabilities"]:
                bits.append("gives: " + ", ".join(row["capabilities"]))
            if not row["connected"]:
                bits.append("NOT CONNECTED")
            else:
                state = row.get("app")
                if state is not None:
                    bits.append("app running" if state.get("reachable") else "app not running")
                if row["can_start"]:
                    bits.append("can be started by Faustus")
                if row["can_show"]:
                    bits.append("can be shown")
            lines.append(" — ".join(bits))
        return {"output": "\n".join(lines), "plugins": rows}


class PluginAppTool:
    """Start one of these applications, and optionally show it."""

    async def execute(self, content: Any, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        from src import plugin_runtime

        args = _args(content)
        ref = str(args.get("plugin") or args.get("name") or args.get("id") or "").strip()
        if not ref:
            return {"error": "say which plugin: pass `plugin` with its id or name "
                             "(plugins_list shows both)."}
        action = str(args.get("action") or "start").strip().lower()
        if action not in ("start", "show", "start_and_show"):
            return {"error": f"unknown action {action!r}; use start, show or start_and_show"}

        owner = _owner(ctx)
        result: Dict[str, Any] = {}
        if action in ("start", "start_and_show"):
            result = await plugin_runtime.ensure_running(ref, owner=owner)
            if not result.get("ok"):
                return {"error": result.get("reason") or "could not start it", **result}
        if action in ("show", "start_and_show"):
            shown = plugin_runtime.show(ref, owner=owner)
            if action == "show" and not shown.get("ok"):
                return {"error": shown.get("reason") or "could not show it", **shown}
            # A window that would not open does not undo a successful start.
            result = {**result, "show": shown}

        detail = result.get("detail") or ""
        shown = result.get("show") or {}
        if shown.get("detail"):
            detail = f"{detail}; {shown['detail']}" if detail else shown["detail"]
        return {"output": detail or "done", **result}
