"""ADP-09 — optional Windows UI Automation backend, over `pywinauto` (BSD-3).

Nothing here is imported at module load time except the standard library:
`pywinauto` (and everything COM-related it drags in) is imported LAZILY,
inside methods, so:

  * this module — and the whole `src.desktop_semantics` package, which
    imports it only from inside `DesktopBackend.semantic()` — stays
    importable on Linux/macOS and on a Windows host without `pywinauto`
    installed;
  * `available()` is the single, cheap, always-safe way to find out whether
    real UIA control is possible right now, mirroring
    `desktop_tools.DesktopBackend.available()`.

This backend NEVER elevates privileges and never claims universal app
compatibility (ADP-09's own limits): a control UIA cannot see (a custom-drawn
canvas, some game engines) simply does not appear in the snapshot — the
model falls back to the coordinate-based `desktop_click`/`desktop_type`
tools for those, same as today.

Physical Windows verification is a DECLARED PENDING for this ADP (see this
lote's final report) — everything here is exercised through
`fake_backend.py` in tests; nothing in this file has run against a real
Windows desktop in this environment (Linux CI).
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

_OPS = ("invoke", "select", "set_value", "scroll", "focus")


def _import_pywinauto():
    """The only place `pywinauto` is imported — see module docstring."""
    import pywinauto  # noqa: F401

    return pywinauto


class WindowsUiaSemanticBackend:
    """Implements the same two-method surface `fake_backend.
    FakeDesktopSemanticBackend` implements for tests:
    `snapshot(session_id=...)` / `invoke(element, op, params, timeout=...)`.
    """

    name = "windows_uia"

    def available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("win"):
            return False, "the Windows UIA backend only runs on Windows"
        try:
            _import_pywinauto()
        except Exception as exc:  # noqa: BLE001 - optional dependency
            return False, (
                f"pywinauto is not installed ({exc}); desktop semantics falls back to "
                "coordinate control only (desktop_click/desktop_type/desktop_key)"
            )
        return True, ""

    def snapshot(self, *, session_id: str, depth: int = 8, max_elements: int = 400) -> Dict[str, Any]:
        ok, reason = self.available()
        if not ok:
            raise RuntimeError(reason)
        from pywinauto import Desktop  # local import — see module docstring

        window = Desktop(backend="uia").window(active_only=True)
        elements: List[Dict[str, Any]] = []
        state = {"truncated": False}

        def walk(ctrl, path: Tuple[int, ...]) -> None:
            if len(elements) >= max_elements or len(path) > depth:
                state["truncated"] = True
                return
            try:
                info = ctrl.element_info
                elements.append({
                    "role": str(getattr(info, "control_type", "") or ""),
                    "name": str(getattr(info, "name", "") or ""),
                    "automation_id": str(getattr(info, "automation_id", "") or ""),
                    "path": list(path),
                    "enabled": bool(ctrl.is_enabled()) if hasattr(ctrl, "is_enabled") else True,
                    "value": _read_value(ctrl),
                })
            except Exception:  # noqa: BLE001 - one bad node must not kill the walk
                return
            try:
                children = ctrl.children()
            except Exception:  # noqa: BLE001
                children = []
            for i, child in enumerate(children):
                walk(child, path + (i,))

        walk(window, ())
        title = ""
        try:
            title = window.window_text()
        except Exception:  # noqa: BLE001
            pass
        app = ""
        try:
            app = str(window.element_info.process_id or "")
        except Exception:  # noqa: BLE001
            pass
        return {"app": app, "window": title, "elements": elements, "truncated": state["truncated"]}

    def invoke(self, element: Any, op: str, params: Dict[str, Any], *,
               timeout: float = 5.0) -> Dict[str, Any]:
        ok, reason = self.available()
        if not ok:
            raise RuntimeError(reason)
        if op not in _OPS:
            return {"delivery": "not_delivered",
                     "observed_after": {"reason": f"unsupported op {op!r}"}, "verified": False}
        # Re-find the live pywinauto wrapper by the SAME identity
        # `session.resolve` matched on — never trust a cached wrapper across
        # calls, a COM reference can go stale the instant the UI changes.
        control = self._find(element)
        if control is None:
            return {"delivery": "not_delivered",
                     "observed_after": {"reason": "control not found"}, "verified": False}
        try:
            if op == "invoke":
                if hasattr(control, "invoke"):
                    control.invoke()
                else:
                    control.click_input()
            elif op == "select":
                control.select(params.get("value"))
            elif op == "set_value":
                control.set_edit_text(params.get("value", ""))
            elif op == "scroll":
                control.scroll(params.get("direction", "down"), params.get("amount", "line"))
            elif op == "focus":
                control.set_focus()
        except Exception as exc:  # noqa: BLE001 - a UIA/COM failure is "not delivered", not a crash
            return {"delivery": "not_delivered", "observed_after": {"error": str(exc)}, "verified": False}
        return {"delivery": "delivered", "observed_after": {"value": _read_value(control)}, "verified": True}

    def _find(self, element: Any):
        from pywinauto import Desktop

        window = Desktop(backend="uia").window(active_only=True)
        return _find_by_identity(window, element, ())


def _read_value(ctrl) -> Optional[str]:
    for attr in ("get_value", "texts"):
        fn = getattr(ctrl, attr, None)
        if fn is None:
            continue
        try:
            value = fn()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return str(value)
    return None


def _find_by_identity(ctrl, element: Any, path: Tuple[int, ...]):
    try:
        info = ctrl.element_info
        role = str(getattr(info, "control_type", "") or "")
        name = str(getattr(info, "name", "") or "")
        automation_id = str(getattr(info, "automation_id", "") or "")
    except Exception:  # noqa: BLE001
        return None
    if (role, name, automation_id, tuple(path)) == (
        element.role, element.name, element.automation_id, tuple(element.path)
    ):
        return ctrl
    try:
        children = ctrl.children()
    except Exception:  # noqa: BLE001
        children = []
    for i, child in enumerate(children):
        found = _find_by_identity(child, element, path + (i,))
        if found is not None:
            return found
    return None
