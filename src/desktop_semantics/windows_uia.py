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

Physical Windows verification covers a synthetic offscreen no-activate
window and writable standard Edit control. It does not establish support for
every UI Automation provider or application.
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

    def snapshot(self, *, session_id: str, depth: int = 8, max_elements: int = 400,
                 target_window: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._snapshot_window(
            session_id=session_id, depth=depth, max_elements=max_elements,
            target_window=target_window,
        )

    def snapshot_targeted(self, *, session_id: str, target_window: Dict[str, Any],
                         depth: int = 8, max_elements: int = 400) -> Dict[str, Any]:
        return self._snapshot_window(
            session_id=session_id, target_window=target_window, depth=depth,
            max_elements=max_elements,
        )

    def _snapshot_window(self, *, session_id: str, depth: int = 8, max_elements: int = 400,
                         target_window: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ok, reason = self.available()
        if not ok:
            raise RuntimeError(reason)
        from pywinauto import Desktop  # local import — see module docstring

        if target_window is None:
            window = Desktop(backend="uia").window(active_only=True)
        else:
            self._validate_target_window(target_window)
            window = Desktop(backend="uia").window(handle=int(target_window["hwnd"]))
            if int(window.element_info.handle) != int(target_window["hwnd"]):
                raise RuntimeError("UIA did not resolve the requested HWND")
            if int(window.element_info.process_id) != int(target_window["pid"]):
                raise RuntimeError("UIA HWND process identity changed")
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
                    **({"target_window": dict(target_window)} if target_window else {}),
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
        raw = {"app": app, "window": title, "elements": elements, "truncated": state["truncated"]}
        if target_window:
            self._validate_target_window(target_window)
            raw["target_window"] = dict(target_window)
        return raw

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
            target_window = element.extra.get("target_window")
            if target_window:
                if op != "set_value":
                    return {"delivery": "not_delivered", "observed_after": {
                        "reason": "targeted background actions only support set_value via ValuePattern"
                    }, "verified": False}
                self._validate_target_window(target_window)
                before = _foreground_and_cursor()
                value_iface = getattr(control, "iface_value", None)
                if value_iface is None:
                    return {"delivery": "not_delivered", "observed_after": {
                        "reason": "control does not expose UIA ValuePattern"
                    }, "verified": False}
                if bool(value_iface.CurrentIsReadOnly):
                    return {"delivery": "not_delivered", "observed_after": {
                        "reason": "UIA ValuePattern is read-only"
                    }, "verified": False}
                value_iface.SetValue(str(params.get("value", "")))
                value = _read_value(control)
                after = _foreground_and_cursor()
                unchanged = before == after
                exact = value == str(params.get("value", ""))
                return {
                    "delivery": "delivered",
                    "observed_after": {
                        "value": value,
                        "foreground_unchanged": before[0] == after[0],
                        "cursor_unchanged": before[1] == after[1],
                        "target_identity_valid": self._target_window_matches(target_window),
                    },
                    "verified": exact and unchanged and self._target_window_matches(target_window),
                }
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

        target_window = element.extra.get("target_window")
        if target_window:
            self._validate_target_window(target_window)
            window = Desktop(backend="uia").window(handle=int(target_window["hwnd"]))
        else:
            window = Desktop(backend="uia").window(active_only=True)
        return _find_by_identity(window, element, ())

    @staticmethod
    def _target_window_matches(target_window: Dict[str, Any]) -> bool:
        try:
            import psutil
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            hwnd = int(target_window["hwnd"])
            user32.IsWindow.argtypes = (wintypes.HWND,)
            user32.IsWindow.restype = wintypes.BOOL
            user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
            user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
            return bool(user32.IsWindow(hwnd)) and int(pid.value) == int(target_window["pid"]) \
                and abs(psutil.Process(int(pid.value)).create_time() - float(target_window["create_time"])) < 0.01
        except Exception:  # noqa: BLE001 - identity cannot be verified, so fail closed
            return False

    def _validate_target_window(self, target_window: Dict[str, Any]) -> None:
        if not self._target_window_matches(target_window):
            raise RuntimeError("target HWND/PID/create_time is stale or no longer matches")


def _foreground_and_cursor():
    import ctypes
    from ctypes import wintypes
    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetCursorPos.argtypes = (ctypes.POINTER(POINT),)
    user32.GetCursorPos.restype = wintypes.BOOL
    point = POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(), "GetCursorPos failed")
    return int(user32.GetForegroundWindow()), (int(point.x), int(point.y))


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
