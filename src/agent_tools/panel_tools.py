"""agent_tools/panel_tools.py -- the eight `panel_*` executors over `src.panels`.

    panel_create    build a new panel (name, html, icon, description)
    panel_update    change a panel's page, name, icon, description or position
    panel_get       read one panel with its HTML
    panel_list      the user's panels
    panel_delete    remove a panel and its versions
    panel_check     render it headless and report console errors, failed requests, screenshot
    panel_versions  list saved versions
    panel_restore   go back to a saved version (written as a new version)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict

from src import panels


def _args(content: Any) -> Dict[str, Any]:
    if isinstance(content, dict):
        return dict(content)
    raw = (content or "").strip() if isinstance(content, str) else ""
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}
    return {}


def _err(tool: str, exc: panels.PanelError) -> Dict[str, Any]:
    return {"error": f"{tool}: {exc.detail}", "error_class": exc.error_class, "exit_code": 1}


def _short(meta: Dict[str, Any]) -> str:
    return f"{meta.get('name')} [{meta.get('id')}] v{meta.get('version')} icon={meta.get('icon')}"


def _wrap(tool: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]], render: Callable[[Dict[str, Any]], str]):
    class _Tool:
        async def execute(self, content: Any, ctx: dict) -> dict:
            args = _args(content)
            try:
                data = await asyncio.to_thread(fn, args)
            except panels.PanelError as exc:
                return _err(tool, exc)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{tool}: {type(exc).__name__}: {exc}", "exit_code": 1}
            return {"output": render(data), "exit_code": 0, "result": data}
    _Tool.__name__ = "".join(p.capitalize() for p in tool.split("_")) + "Tool"
    return _Tool


def _need_id(a: Dict[str, Any]) -> str:
    pid = str(a.get("id") or a.get("panel_id") or "").strip()
    if not pid:
        raise panels.PanelError("`id` is required")
    return pid


def _create(a):
    return panels.create(a.get("name"), a.get("html"), icon=a.get("icon"), description=a.get("description", ""),
                         panel_id=a.get("id") or None)


def _update(a):
    return panels.update(_need_id(a), html=a.get("html"), name=a.get("name"), icon=a.get("icon"),
                         description=a.get("description"), order=a.get("order"))


def _get(a):
    return panels.get(_need_id(a))


def _list(a):
    return {"panels": panels.list_panels()}


def _delete(a):
    return panels.delete(_need_id(a))


def _check(a):
    from core.middleware import INTERNAL_TOOL_TOKEN
    return panels.check(_need_id(a), base_url=a.get("base_url") or None, wait_ms=int(a.get("wait_ms") or 1500),
                        token=INTERNAL_TOOL_TOKEN)


def _versions(a):
    return panels.versions(_need_id(a))


def _restore(a):
    return panels.restore(_need_id(a), a.get("version"))


def _r_check(d: Dict[str, Any]) -> str:
    lines = [f"panel {d['id']}: {'OK' if d['ok'] else 'PROBLEMS'} (HTTP {d['status']}, {d['visible_text_chars']} visible characters, title {d['title']!r})"]
    for e in d["console_errors"]:
        lines.append(f"console error: {e['text']}")
    for e in d["page_errors"]:
        lines.append(f"page error: {e}")
    for f in d["failed_requests"]:
        lines.append(f"failed request: {f.get('status') or f.get('error')} {f['url']}")
    lines += [f"note: {n}" for n in d["notes"]]
    if d.get("screenshot"):
        lines.append(f"screenshot: {d['screenshot']}")
    return "\n".join(lines)


PanelCreateTool = _wrap("panel_create", _create, lambda d: f"created {_short(d)}; it now shows in the sidebar under Panels")
PanelUpdateTool = _wrap("panel_update", _update, lambda d: f"updated {_short(d)} (html {'changed' if d.get('html_changed') else 'unchanged'})")
PanelGetTool = _wrap("panel_get", _get, lambda d: f"{_short(d)}\n\n{d.get('html', '')}")
PanelListTool = _wrap("panel_list", _list, lambda d: "\n".join(_short(p) for p in d["panels"]) or "no panels yet")
PanelDeleteTool = _wrap("panel_delete", _delete, lambda d: f"deleted panel {d['deleted']}")
PanelCheckTool = _wrap("panel_check", _check, _r_check)
PanelVersionsTool = _wrap("panel_versions", _versions, lambda d: f"current v{d['current']}\n" + "\n".join(
    f"v{v['version']}  {v['saved']}  {v['bytes']} bytes" for v in d["versions"]))
PanelRestoreTool = _wrap("panel_restore", _restore, lambda d: f"restored v{d['restored_from']} as {_short(d)}")

HANDLERS = {
    "panel_create": PanelCreateTool, "panel_update": PanelUpdateTool, "panel_get": PanelGetTool,
    "panel_list": PanelListTool, "panel_delete": PanelDeleteTool, "panel_check": PanelCheckTool,
    "panel_versions": PanelVersionsTool, "panel_restore": PanelRestoreTool,
}
