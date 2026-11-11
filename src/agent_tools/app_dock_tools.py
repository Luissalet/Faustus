"""agent_tools/app_dock_tools.py -- the three App Dock tools (src/app_dock.py).

    app_open   dock a Hoard beside the chat (by id or name), starting it through the Hub when it is down
    app_view   what the person has on screen in the docked app, the outline of the page and the app's own tools
    app_act    one action in the docked page (click, fill, select, navigate, scroll, highlight, wait) or one of the app's own tools

The person sees the actor as a named cursor in the page. Without a host (Studio not showing this chat) a
call answers `no_host` at once.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from src import app_dock

_GENERIC = {
    "click": "app_click", "fill": "app_fill", "select": "app_select", "navigate": "app_navigate",
    "scroll": "app_scroll", "highlight": "app_highlight", "wait": "app_wait", "read": "app_read",
}
_SCROLL_BY = {"down", "up", "top", "bottom"}


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
    return {"app": raw} if raw else {}


def _fail(tool: str, code: str, message: str) -> Dict[str, Any]:
    return {"error": f"{tool}: {message}", "error_class": "app_dock." + code, "exit_code": 1}


def _page_args(action: str, a: Dict[str, Any]) -> Dict[str, Any]:
    """Arguments for the bridge's generic tool from the flat `target` / `value` the model gives."""
    target = a.get("target")
    value = a.get("value")
    out: Dict[str, Any] = {}
    t = "" if target is None else str(target)
    if action in ("click", "highlight"):
        if t:
            out.update(ref=t, text=t)
        if action == "highlight" and value:
            out["note"] = str(value)
    elif action == "fill":
        if t:
            out.update(ref=t, label=t)
        if value is not None:
            out["value"] = str(value)
    elif action == "select":
        if t:
            out.update(ref=t, label=t)
        if value is not None:
            out["option"] = str(value)
    elif action == "navigate":
        out["to"] = t or str(value or "")
    elif action == "scroll":
        if str(value or "").lower() in _SCROLL_BY:
            out["by"] = str(value).lower()
        elif t.lower() in _SCROLL_BY:
            out["by"] = t.lower()
        if t and "by" not in out:
            out.update(ref=t, text=t)
    elif action == "wait":
        if value is not None and str(value).isdigit():
            out["ms"] = int(value)
        elif t or value:
            out["text"] = t or str(value)
    elif action == "read":
        if t in ("screen", "page"):
            out["scope"] = t
    extra = a.get("args")
    if isinstance(extra, dict):
        out.update(extra)
    return out


_OUTLINE_CAP = 6000   # the bridge's default `max_chars` for app_read
_NARROW_PX = 700


def _outline_text(outline: Any) -> str:
    if isinstance(outline, str):
        return outline
    if isinstance(outline, dict):
        return str(outline.get("text") or outline.get("outline") or "")
    return ""


def _view_note(scope: str, text: str, page_text: Optional[str], viewport: Any) -> str:
    """One line telling the model the outline may be incomplete, and why: cut at the cap, only the visible
    screen (with how much more the whole page has), or a panel narrow enough for the app to hide parts."""
    bits: List[str] = []
    if len(text) >= _OUTLINE_CAP - 40:
        bits.append(f"the outline was cut at {_OUTLINE_CAP} characters (app_act action=read with args max_chars for more)")
    if scope == "screen" and page_text is not None:
        extra = len([ln for ln in page_text.splitlines() if ln.strip()]) - len([ln for ln in text.splitlines() if ln.strip()])
        if extra > 0:
            bits.append(f"{extra} more lines exist outside the visible area (scroll, or call app_view with scope \"page\")")
    w = (viewport or {}).get("w") if isinstance(viewport, dict) else None
    if isinstance(w, int) and w < _NARROW_PX:
        bits.append(f"the panel is only {w}px wide, so the app may be hiding parts of its layout (ask the person to widen or maximise it)")
    return ("Note: " + "; ".join(bits) + ".") if bits else ""


def _render_view(snap: Dict[str, Any], outline: Any, note: str = "") -> str:
    ctx = snap.get("context") or {}
    lines = [f"App docked: {snap.get('name')} ({snap.get('app_id')})"]
    for key in ("title", "view", "url", "selection", "summary"):
        if ctx.get(key):
            lines.append(f"{key}: {ctx[key]}")
    if isinstance(ctx.get("focus"), dict) and ctx["focus"]:
        lines.append("focus: " + json.dumps(ctx["focus"], ensure_ascii=False))
    text = outline if isinstance(outline, str) else (outline.get("text") or outline.get("outline") or json.dumps(outline, ensure_ascii=False)) if isinstance(outline, dict) else ""
    if text:
        lines += ["", "Screen:", str(text)]
    if note:
        lines += ["", note]
    tools = snap.get("tools") or []
    own = [t for t in tools if not str(t.get("name", "")).startswith("app_")]
    if own:
        lines += ["", "The app's own tools (app_act action=<name>, args={...}):"]
        lines += [f"- {t['name']}: {t.get('description', '')[:120]}" for t in own[:40]]
    return "\n".join(lines)


class AppOpenTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        name = str(a.get("app") or a.get("id") or a.get("name") or "").strip()
        if not name:
            return _fail("app_open", "bad_args", "`app` is required (an app id or name)")
        sid = app_dock.resolve_session((ctx or {}).get("session_id"))
        if not sid:
            return _fail("app_open", "bad_args", "this call has no conversation to dock the app in")
        try:
            snap = await app_dock.open_app(sid, name)
        except app_dock.DockError as exc:
            return _fail("app_open", exc.code, exc.message)
        names = [t["name"] for t in snap.get("tools") or [] if not t["name"].startswith("app_")]
        out = [f"{snap['name']} is open beside the chat" + (" (started through the Hub)" if snap.get("started") else "") + "."]
        if not snap.get("ready"):
            out.append("Its page has not answered the handshake yet (loading, no Studio window showing this chat, or no bridge): "
                       "call app_view in a moment.")
        elif names:
            out.append("Its own tools: " + ", ".join(names[:20]))
        out.append("Use app_view to see the screen and app_act to act on it.")
        return {"output": " ".join(out), "exit_code": 0, "result": snap}


class AppViewTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        sid = app_dock.resolve_session((ctx or {}).get("session_id"))
        snap = app_dock.get(sid)
        if not snap.get("app_id"):
            return _fail("app_view", "no_app", "no app is docked in this conversation; call app_open first")
        scope = "page" if str(a.get("scope") or "").lower() == "page" else "screen"
        res = await app_dock.call(sid, "app_read", {"scope": scope}, app_dock.actor_for((ctx or {}).get("session_id")))
        snap = app_dock.get(sid)
        if not res.get("ok"):
            e = res.get("error") or {}
            return _fail("app_view", str(e.get("code") or "error"), str(e.get("message") or "the app did not answer"))
        text = _outline_text(res.get("result"))
        page_text: Optional[str] = None
        if scope == "screen":
            # Compare with the whole page to know whether the visible screen left something out.
            try:
                full = await app_dock.call(sid, "app_read", {"scope": "page"}, app_dock.actor_for((ctx or {}).get("session_id")))
                if full.get("ok"):
                    page_text = _outline_text(full.get("result"))
            except Exception:  # noqa: BLE001
                page_text = None
        note = _view_note(scope, text, page_text, snap.get("viewport"))
        return {"output": _render_view(snap, res.get("result"), note), "exit_code": 0, "result": {**snap, "outline": res.get("result")}}


class AppActTool:
    async def execute(self, content: Any, ctx: dict) -> dict:
        a = _args(content)
        action = str(a.get("action") or "").strip()
        if not action:
            return _fail("app_act", "bad_args", "`action` is required (click, fill, select, navigate, scroll, highlight, wait, or an app tool name)")
        sid = app_dock.resolve_session((ctx or {}).get("session_id"))
        if not app_dock.get(sid).get("app_id"):
            return _fail("app_act", "no_app", "no app is docked in this conversation; call app_open first")
        low = action.lower()
        if low in _GENERIC:
            tool, args = _GENERIC[low], _page_args(low, a)
        else:
            tool = action
            args = a.get("args") if isinstance(a.get("args"), dict) else {}
        res = await app_dock.call(sid, tool, args, app_dock.actor_for((ctx or {}).get("session_id")))
        if not res.get("ok"):
            e = res.get("error") or {}
            msg = str(e.get("message") or "the app did not answer")
            extra = {k: v for k, v in e.items() if k not in ("code", "message")}
            if extra:
                msg += " " + json.dumps(extra, ensure_ascii=False)[:600]
            return _fail("app_act", str(e.get("code") or "error"), msg)
        result = res.get("result")
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return {"output": f"{action}: {text[:4000]}", "exit_code": 0, "result": result}


HANDLERS = {"app_open": AppOpenTool, "app_view": AppViewTool, "app_act": AppActTool}
