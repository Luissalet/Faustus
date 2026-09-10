"""DESK-01 — visible desktop control: per-session allowlist + audit trail.

Both are OPT-IN additions to ``src/desktop_control_session.py`` /
``src/agent_tools/desktop_tools.py``: a session that never calls
``set_allowlist``/``enable_audit`` gets today's unrestricted behaviour
unchanged (see the full existing suite in ``tests/test_desktop_tools.py``,
which never calls either and must keep passing unmodified).
"""
import asyncio

import pytest

from src import desktop_control_session as control
from src.agent_tools import desktop_tools as dt


class FakeBackend(dt.DesktopBackend):
    name = "fake"

    def __init__(self, foreground_title="Notepad"):
        self.calls = []
        self._foreground = foreground_title
        self._windows = [
            {"title": "Notepad", "handle": 1, "rect": [0, 0, 100, 100], "foreground": foreground_title == "Notepad"},
            {"title": "Mozilla Firefox", "handle": 2, "rect": [0, 0, 100, 100], "foreground": foreground_title == "Mozilla Firefox"},
        ]

    def available(self):
        return True, ""

    def screen_size(self):
        return 100, 100

    def list_monitors(self):
        return [{"index": 0, "left": 0, "top": 0, "width": 100, "height": 100, "primary": True}]

    def grab(self, region):
        self.calls.append(("grab", region))
        from PIL import Image
        return Image.new("RGB", (region[2], region[3]), (30, 60, 90))

    def list_windows(self):
        self.calls.append(("list_windows",))
        return [dict(w) for w in self._windows]

    def focus_window(self, title):
        for w in self._windows:
            if title.lower() in w["title"].lower():
                return w
        raise dt.DesktopError(f"no window matches {title!r}")

    def click(self, x, y, button):
        self.calls.append(("click", x, y, button))

    def type_text(self, text):
        self.calls.append(("type_text", text))

    def key_combo(self, keys):
        self.calls.append(("key_combo", tuple(keys)))

    def scroll(self, x, y, dy):
        self.calls.append(("scroll", x, y, dy))


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    control.reset_desk01_state()
    dt.reset_capture_state()
    monkeypatch.setattr(dt, "_control_mode_guard", lambda _tool: None)
    yield
    control.reset_desk01_state()


def _click(backend, session="s1"):
    return asyncio.run(dt.DesktopTool("desktop_click").execute('{"x": 1, "y": 1}', {"session_id": session}))


# ---------------------------------------------------------------------------
# Allowlist: pure functions
# ---------------------------------------------------------------------------

def test_is_window_allowed_matches_case_insensitive_substring():
    assert control.is_window_allowed("Mozilla Firefox", ["firefox"]) is True
    assert control.is_window_allowed("Notepad", ["firefox"]) is False
    assert control.is_window_allowed("anything", []) is True  # no policy configured


def test_check_focus_authorized_fails_open_when_title_unknown():
    assert control.check_focus_authorized(None, ["firefox"]) is None


# ---------------------------------------------------------------------------
# Allowlist wired through DesktopTool._execute (opt-in)
# ---------------------------------------------------------------------------

def test_no_allowlist_configured_leaves_existing_behaviour_untouched(monkeypatch):
    backend = FakeBackend(foreground_title="Mozilla Firefox")
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    _, result = _click(backend, session="s1")
    assert result["exit_code"] == 0
    assert ("click", 1, 1, "left") in backend.calls


def test_allowlisted_foreground_window_is_allowed(monkeypatch):
    backend = FakeBackend(foreground_title="Notepad")
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    control.set_allowlist("s1", ["Notepad"])
    _, result = _click(backend, session="s1")
    assert result["exit_code"] == 0


def test_regression_focus_on_unauthorized_app_pauses_automation(monkeypatch):
    """The DESK-01 acceptance criterion itself: moving focus to an app not on
    the allowlist must pause (refuse) the next control action. Without
    `check_focus_authorized` wired into `DesktopTool._execute`, this click
    would silently succeed against the wrong app."""
    backend = FakeBackend(foreground_title="Mozilla Firefox")
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    control.set_allowlist("s1", ["Notepad"])
    _, result = _click(backend, session="s1")
    assert result["exit_code"] == 1
    assert "paused" in result["error"].lower()
    assert "Firefox" in result["error"]
    assert not any(c[0] == "click" for c in backend.calls)


def test_allowlist_is_scoped_per_session(monkeypatch):
    backend = FakeBackend(foreground_title="Mozilla Firefox")
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    control.set_allowlist("s1", ["Notepad"])
    # A different session with no allowlist configured is unaffected.
    _, result = _click(backend, session="other-session")
    assert result["exit_code"] == 0


def test_clear_allowlist_restores_unrestricted_behaviour(monkeypatch):
    backend = FakeBackend(foreground_title="Mozilla Firefox")
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    control.set_allowlist("s1", ["Notepad"])
    control.clear_allowlist("s1")
    _, result = _click(backend, session="s1")
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Audit trail: before/after capture + registro for every control action
# ---------------------------------------------------------------------------

def test_audit_disabled_by_default_records_nothing(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    _click(backend, session="s1")
    assert control.audit_log("s1") == []
    assert not any(c[0] == "grab" for c in backend.calls)


def test_audit_enabled_records_before_after_hash_and_tool_name(monkeypatch, tmp_path):
    monkeypatch.setattr(control, "RUNTIME", tmp_path)
    backend = FakeBackend()
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    control.enable_audit("s1")
    _, result = _click(backend, session="s1")
    assert result["exit_code"] == 0
    entries = control.audit_log("s1")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "desktop_click"
    assert entry["before_hash"] and entry["after_hash"]
    assert entry["session_id"] == "s1"
    # persisted to disk too
    logged = (tmp_path / "desktop-audit.log").read_text(encoding="utf-8")
    assert "desktop_click" in logged


def test_disable_audit_stops_recording(monkeypatch):
    backend = FakeBackend()
    monkeypatch.setattr(dt, "get_backend", lambda: backend)
    control.enable_audit("s1")
    control.disable_audit("s1")
    _click(backend, session="s1")
    assert control.audit_log("s1") == []
