"""ADP-08/ADP-09 — deterministic in-memory desktop app for tests.

No display, no `pywinauto`, no real OS: `FakeDesktopSemanticBackend`
implements the same two-method surface `windows_uia.py` implements for
real (`snapshot(session_id=...)`, `invoke(element, op, params, timeout=...)`)
over a small in-memory control tree the test itself builds. Four knobs cover
the scenarios the ADP-08/ADP-09 fichas name as acceptance cases:

  duplicate controls   `add_control` twice with the same (role, name) —
                        `resolve`/`desktop_find` must not silently pick one.
  window swap           `swap_window()` — the NEXT snapshot gets a new
                        generation, so refs cut before the swap go stale.
  deleted control        `delete_control()` — a ref that named it degrades to
                        a typed error, not a crash or a click on nothing.
  delayed response       `set_delay(op, seconds)` beyond the caller's timeout
                        — `invoke()` raises `TimeoutError`, the same signal
                        `session.act()` turns into `delivery="unknown"`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple


class FakeControl:
    """One element of the fake app's control tree."""

    def __init__(
        self,
        role: str,
        name: str,
        automation_id: str = "",
        path: Tuple[int, ...] = (),
        enabled: bool = True,
        value: Optional[str] = None,
    ):
        self.role = role
        self.name = name
        self.automation_id = automation_id
        self.path = tuple(path)
        self.enabled = enabled
        self.value = value
        self.click_count = 0

    def identity(self) -> Tuple[str, str, str, Tuple[int, ...]]:
        return (self.role, self.name, self.automation_id, self.path)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "role": self.role,
            "name": self.name,
            "automation_id": self.automation_id,
            "path": list(self.path),
            "enabled": self.enabled,
            "value": self.value,
        }


class FakeWindow:
    def __init__(self, app: str, title: str, controls: Optional[List[FakeControl]] = None):
        self.app = app
        self.title = title
        self.controls: List[FakeControl] = list(controls or [])


class FakeDesktopSemanticBackend:
    """A `desktop_semantics` backend a test can shape by hand.

    `invocations` records every `invoke()` call in order (op, target
    identity, timestamp) — tests assert against it directly to prove an
    `unknown`-delivery action was never retried automatically.
    """

    def __init__(self, app: str = "FakeApp", title: str = "Fake Window"):
        self._lock = threading.Lock()
        self.window = FakeWindow(app, title)
        self._delays: Dict[str, float] = {}
        self._responses: Dict[str, Dict[str, Any]] = {}
        self.invocations: List[Dict[str, Any]] = []

    # -- available() matches windows_uia.WindowsUiaSemanticBackend's shape --
    def available(self) -> Tuple[bool, str]:
        return True, ""

    # -- test setup -----------------------------------------------------
    def add_control(
        self, role: str, name: str, automation_id: str = "", path: Tuple[int, ...] = (),
        enabled: bool = True, value: Optional[str] = None,
    ) -> FakeControl:
        with self._lock:
            control = FakeControl(role, name, automation_id, path, enabled, value)
            self.window.controls.append(control)
            return control

    def swap_window(self, title: str, controls: Optional[List[FakeControl]] = None) -> None:
        """Simulates the user switching window/app: the NEXT `snapshot()`
        reports a different (app, window), which is what bumps the
        session's generation in `session.take_snapshot`."""
        with self._lock:
            self.window = FakeWindow(self.window.app, title, controls)

    def delete_control(self, control: FakeControl) -> None:
        with self._lock:
            if control in self.window.controls:
                self.window.controls.remove(control)

    def set_delay(self, op: str, seconds: float) -> None:
        self._delays[op] = seconds

    def set_response(self, op: str, response: Dict[str, Any]) -> None:
        self._responses[op] = response

    # -- desktop_semantics backend surface -------------------------------
    def snapshot(self, *, session_id: str, depth: Optional[int] = None,
                 max_elements: int = 500) -> Dict[str, Any]:
        with self._lock:
            controls = list(self.window.controls)
            truncated = len(controls) > max_elements
            controls = controls[:max_elements]
            return {
                "app": self.window.app,
                "window": self.window.title,
                "elements": [c.to_dict() for c in controls],
                "truncated": truncated,
            }

    def invoke(self, element: Any, op: str, params: Dict[str, Any], *,
               timeout: float = 5.0) -> Dict[str, Any]:
        delay = self._delays.get(op, 0.0)
        self.invocations.append({
            "op": op, "role": element.role, "name": element.name,
            "path": element.path, "at": time.time(),
        })
        if delay > timeout:
            # A real hung UIA call never returns within the caller's
            # budget; simulate exactly that instead of returning early.
            time.sleep(min(delay, timeout + 0.05))
            raise TimeoutError(f"{op} on {element.name!r} did not respond within {timeout}s")
        if delay:
            time.sleep(delay)
        control = self._find_live(element)
        if control is None:
            return {"delivery": "not_delivered",
                     "observed_after": {"reason": "control no longer present"}, "verified": False}
        if not control.enabled:
            return {"delivery": "not_delivered",
                     "observed_after": {"reason": "control disabled"}, "verified": False}
        if op == "invoke":
            control.click_count += 1
        elif op == "set_value":
            control.value = params.get("value")
        elif op == "select":
            control.value = params.get("value", control.value)
        elif op in ("scroll", "focus"):
            pass
        else:
            return {"delivery": "not_delivered",
                     "observed_after": {"reason": f"unsupported op {op!r}"}, "verified": False}
        response = dict(self._responses.get(op) or {})
        response.setdefault("delivery", "delivered")
        response.setdefault("observed_after", control.to_dict())
        response.setdefault("verified", True)
        return response

    def _find_live(self, element: Any) -> Optional[FakeControl]:
        wanted = (element.role, element.name, element.automation_id, tuple(element.path))
        with self._lock:
            for control in self.window.controls:
                if control.identity() == wanted:
                    return control
        return None


class FakeDesktopBackend:
    """A `DesktopBackend`-shaped object (see
    `src/agent_tools/desktop_tools.py::DesktopBackend`) whose only purpose
    is `.semantic()` — the optional capability ADP-08 adds. Tests use this
    instead of a real platform backend subclass, so no display/xdotool/
    pywinauto is ever required."""

    name = "fake"

    def __init__(self, semantic: Optional[FakeDesktopSemanticBackend] = None):
        self._semantic = semantic or FakeDesktopSemanticBackend()

    def available(self) -> Tuple[bool, str]:
        return True, ""

    def semantic(self) -> FakeDesktopSemanticBackend:
        return self._semantic
