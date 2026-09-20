"""Dictate anywhere core logic (src/dictation_paste.py): clipboard
save/restore, foreground capture/refocus, and the clipboard/type paste
paths — all against fake backends so this runs on any platform, plus a
platform guard proving every entry point refuses cleanly off Windows.
"""
from __future__ import annotations

import pytest

from src import dictation_paste as dp


# ── Fakes ───────────────────────────────────────────────────────────────

class FakeClipboard:
    def __init__(self, initial: str | None = None, other_formats: bool = False):
        self.text = initial
        self.other_formats = other_formats
        self.writes: list[str] = []

    def read_text(self):
        return self.text

    def write_text(self, text):
        self.text = text
        self.writes.append(text)

    def has_non_text_formats(self):
        return self.other_formats


class FakeWin:
    """Stands in for ``src.agent_tools.desktop_tools.WindowsBackend`` --
    only the surface dictation_paste actually calls."""

    def __init__(self, foreground: int = 100):
        self.foreground = foreground
        self.typed: list[str] = []
        self.combos: list[list[str]] = []
        self.focus_calls: list[dict] = []
        self.focus_should_work = True
        self.window_title = "Notepad"

        class _Ctypes:
            @staticmethod
            def create_unicode_buffer(n):
                return _Buf(n)

            @staticmethod
            def wstring_at(ptr):
                return "unused"

        class _Buf:
            def __init__(self, n):
                self.value = ""

        self.ctypes = _Ctypes()

    # user32.* surface, called as win.user32.X(...)
    class _User32:
        def __init__(self, outer):
            self.outer = outer

        def GetForegroundWindow(self):
            return self.outer.foreground

        def GetWindowTextLengthW(self, hwnd):
            return len(self.outer.window_title)

        def GetWindowTextW(self, hwnd, buf, n):
            buf.value = self.outer.window_title

    @property
    def user32(self):
        return FakeWin._User32(self)

    def focus_target(self, target: dict):
        self.focus_calls.append(target)
        if not self.focus_should_work:
            raise dp.DictationError("could not focus")
        self.foreground = target["handle"]

    def type_text(self, text):
        self.typed.append(text)

    def key_combo(self, keys):
        self.combos.append(list(keys))


# ── Platform guard ──────────────────────────────────────────────────────

@pytest.mark.skipif(dp.IS_WINDOWS, reason="asserts the non-Windows guard path")
def test_every_entry_point_refuses_off_windows():
    with pytest.raises(dp.DictationUnsupported):
        dp.capture_foreground()
    with pytest.raises(dp.DictationUnsupported):
        dp.refocus_target(dp.ForegroundTarget(handle=1))
    with pytest.raises(dp.DictationUnsupported):
        dp.paste_text("hello")
    with pytest.raises(dp.DictationUnsupported):
        dp.Win32Clipboard()


# ── ForegroundTarget (de)serialization ──────────────────────────────────

def test_foreground_target_round_trip():
    target = dp.ForegroundTarget(handle=42, title="Notepad")
    assert dp.ForegroundTarget.from_dict(target.as_dict()) == target


def test_foreground_target_from_dict_rejects_bad_handle():
    with pytest.raises(dp.DictationError):
        dp.ForegroundTarget.from_dict({"handle": "not-a-number"})
    with pytest.raises(dp.DictationError):
        dp.ForegroundTarget.from_dict({})


# ── capture_foreground / refocus_target (fake win, force-enabled) ──────

def test_capture_foreground_reads_handle_and_title(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=777)
    win.window_title = "Visual Studio Code"
    target = dp.capture_foreground(win=win)
    assert target.handle == 777
    assert target.title == "Visual Studio Code"


def test_refocus_is_a_no_op_when_already_foreground(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=5)
    target = dp.ForegroundTarget(handle=5, title="x")
    assert dp.refocus_target(target, win=win) is True
    assert win.focus_calls == []  # never needed to call focus_target


def test_refocus_brings_target_back_when_focus_drifted(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=999)  # e.g. Faustus's own window stole focus
    target = dp.ForegroundTarget(handle=5, title="Notepad")
    assert dp.refocus_target(target, win=win) is True
    assert win.focus_calls == [{"handle": 5, "title": "Notepad"}]
    assert win.foreground == 5


def test_refocus_reports_failure_without_raising(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=999)
    win.focus_should_work = False
    target = dp.ForegroundTarget(handle=5, title="Notepad")
    assert dp.refocus_target(target, win=win) is False


# ── paste_text: clipboard method ────────────────────────────────────────

def test_paste_clipboard_saves_and_restores(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=5)
    clipboard = FakeClipboard(initial="what was there before")
    result = dp.paste_text(
        "hola mundo", clipboard=clipboard, win=win, settle_s=0,
    )
    assert clipboard.writes == ["hola mundo", "what was there before"]
    assert clipboard.text == "what was there before"
    assert win.combos == [["ctrl", "v"]]
    assert result["method"] == "clipboard"
    assert result["clipboard_restored"] is True
    assert result["note"] is None


def test_paste_clipboard_notes_lost_non_text_format(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=5)
    clipboard = FakeClipboard(initial=None, other_formats=True)  # e.g. an image was on it
    result = dp.paste_text("hola", clipboard=clipboard, win=win, settle_s=0)
    assert result["clipboard_restored"] is False
    assert result["note"] and "non-text format" in result["note"]


def test_paste_clipboard_without_restore_leaves_dictated_text(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=5)
    clipboard = FakeClipboard(initial="previous")
    dp.paste_text("dictated", clipboard=clipboard, win=win, restore_clipboard=False, settle_s=0)
    assert clipboard.text == "dictated"
    assert clipboard.writes == ["dictated"]


def test_paste_refocuses_captured_target_before_pasting(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    # Focus drifted to something else (e.g. Faustus's own window) while
    # recording; the paste must land back on the originally captured app.
    win = FakeWin(foreground=999)
    clipboard = FakeClipboard(initial="")
    target = dp.ForegroundTarget(handle=5, title="Notepad")
    result = dp.paste_text("dictated text", target=target, clipboard=clipboard, win=win, settle_s=0)
    assert result["refocused"] is True
    assert win.foreground == 5
    assert win.combos == [["ctrl", "v"]]


def test_paste_never_pastes_into_wrong_window_when_refocus_fails(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=999)
    win.focus_should_work = False
    target = dp.ForegroundTarget(handle=5, title="Notepad")
    with pytest.raises(dp.DictationError):
        dp.paste_text("dictated text", target=target, win=win, settle_s=0)
    assert win.combos == []  # never sent Ctrl+V anywhere


# ── paste_text: type method ─────────────────────────────────────────────

def test_paste_type_method_never_touches_clipboard(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    win = FakeWin(foreground=5)
    clipboard = FakeClipboard(initial="untouched")
    result = dp.paste_text("typed text", method="type", clipboard=clipboard, win=win, settle_s=0)
    assert win.typed == ["typed text"]
    assert clipboard.writes == []
    assert clipboard.text == "untouched"
    assert result["method"] == "type"
    assert result["clipboard_restored"] is None


# ── input validation ──────────────────────────────────────────────────

def test_paste_rejects_empty_text(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    with pytest.raises(dp.DictationError):
        dp.paste_text("", win=FakeWin())


def test_paste_rejects_unknown_method(monkeypatch):
    monkeypatch.setattr(dp, "_require_windows", lambda: None)
    with pytest.raises(dp.DictationError):
        dp.paste_text("hi", method="voice", win=FakeWin())
