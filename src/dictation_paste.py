# src/dictation_paste.py
"""Push-to-talk "dictate anywhere": land dictated text in whatever app has
focus, not just Studio's own composer.

The flow (driven by ``routes/dictation_routes.py``): the desktop shell
captures the foreground window the instant the push-to-talk hotkey goes
down (:func:`capture_foreground`, *before* recording starts, so the target
is the app the user was actually looking at — never Faustus's own window
answering the capture call itself). It records while the key is held, then
posts the audio for transcription; the resulting text is delivered with
:func:`paste_text`, which re-aims at that captured target if focus drifted
away from it in the meantime (most likely to Faustus's own window) before
doing the paste, so Faustus's own composer never receives text meant for
another app.

Two delivery methods:
  * ``"clipboard"`` (default) — save the current clipboard text, put the
    dictated text on it, send Ctrl+V to the foreground window via
    ``SendInput`` (the same primitive ``src/agent_tools/desktop_tools.py``'s
    ``WindowsBackend`` uses for the agent's own desktop control — no new
    dependency), then restore what was there before.
  * ``"type"`` — types the text directly via ``SendInput`` Unicode events,
    for apps that block synthetic paste (some terminals, some games,
    password managers).

Windows-only. Every public entry point raises :class:`DictationUnsupported`
immediately on any other platform, so importing this module and calling it
from a non-Windows CI box degrades to a clean error instead of an
``AttributeError`` deep inside a ``ctypes.WinDLL`` call.

Clipboard preservation is best-effort and says so: only ``CF_UNICODETEXT``
is saved and restored. If the clipboard held something else (an image, rich
HTML, a file-copy list) when dictation ran, that format is lost — a
documented limitation of the Win32 clipboard API (there is no "read
everything, write it all back" call), not a bug. Callers get that fact
back in the result's ``note`` field rather than silent data loss.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

IS_WINDOWS = sys.platform == "win32"

# Win32 clipboard format ids that carry text-ish data we don't clobber
# without noticing. CF_TEXT=1, CF_UNICODETEXT=13, CF_LOCALE=16 (Windows
# tags CF_TEXT with a CF_LOCALE entry automatically; it isn't "another
# format" from the user's point of view).
_CF_TEXT = 1
_CF_UNICODETEXT = 13
_CF_LOCALE = 16
_TEXT_LIKE_FORMATS = frozenset({_CF_TEXT, _CF_UNICODETEXT, _CF_LOCALE})


class DictationError(Exception):
    """A dictate-anywhere step failed; the message is safe to show the user."""


class DictationUnsupported(DictationError):
    """Dictate-anywhere was asked for on a platform that isn't Windows."""

    def __init__(self, detail: str = "Dictate anywhere needs Windows (SendInput + the clipboard API)."):
        super().__init__(detail)


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise DictationUnsupported()


@dataclass
class ForegroundTarget:
    """A window captured at push-to-talk key-down: enough to re-identify
    and re-focus it later, even if something else took focus meanwhile."""

    handle: int
    title: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"handle": self.handle, "title": self.title}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ForegroundTarget":
        try:
            handle = int(data["handle"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DictationError("target.handle must be an integer window handle") from exc
        return cls(handle=handle, title=str(data.get("title") or ""))


# ── Win32 clipboard (CF_UNICODETEXT only) ──────────────────────────────────

class Win32Clipboard:
    """Real Win32 clipboard access, text-only. Built lazily so importing
    this module never touches ``ctypes.WinDLL`` off Windows."""

    GMEM_MOVEABLE = 0x0002

    def __init__(self):
        _require_windows()
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _open(self) -> None:
        for _ in range(25):
            if self.user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise DictationError("Could not open the Windows clipboard (another app is holding it)")

    def read_text(self) -> Optional[str]:
        self._open()
        try:
            handle = self.user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return None
            locked = self.kernel32.GlobalLock(handle)
            if not locked:
                return None
            try:
                return self.ctypes.wstring_at(locked)
            finally:
                self.kernel32.GlobalUnlock(handle)
        finally:
            self.user32.CloseClipboard()

    def write_text(self, text: str) -> None:
        data = text.encode("utf-16-le") + b"\x00\x00"
        block = self.kernel32.GlobalAlloc(self.GMEM_MOVEABLE, len(data))
        if not block:
            raise DictationError("GlobalAlloc failed while preparing the clipboard")
        ptr = self.kernel32.GlobalLock(block)
        if not ptr:
            raise DictationError("GlobalLock failed while preparing the clipboard")
        try:
            self.ctypes.memmove(ptr, data, len(data))
        finally:
            self.kernel32.GlobalUnlock(block)
        self._open()
        try:
            self.user32.EmptyClipboard()
            if not self.user32.SetClipboardData(_CF_UNICODETEXT, block):
                raise DictationError("SetClipboardData failed")
        finally:
            self.user32.CloseClipboard()

    def has_non_text_formats(self) -> bool:
        """True when the clipboard holds a format besides plain/Unicode
        text — the signal that a restore will not be a complete one."""
        self._open()
        try:
            fmt = 0
            while True:
                fmt = self.user32.EnumClipboardFormats(fmt)
                if not fmt:
                    return False
                if fmt not in _TEXT_LIKE_FORMATS:
                    return True
        finally:
            self.user32.CloseClipboard()


def _default_clipboard() -> Win32Clipboard:
    return Win32Clipboard()


def _default_win():
    from src.agent_tools.desktop_tools import WindowsBackend

    return WindowsBackend()


# ── Foreground window capture / refocus ────────────────────────────────────

def capture_foreground(win: Any = None) -> ForegroundTarget:
    """Capture the current foreground window. Call this the instant
    push-to-talk fires, before recording starts — that's what makes the
    later paste land in the app the user meant, not in whatever has focus
    once transcription finishes."""
    _require_windows()
    win = win or _default_win()
    hwnd = win.user32.GetForegroundWindow()
    if not hwnd:
        raise DictationError("No foreground window to capture")
    handle = int(hwnd)
    length = win.user32.GetWindowTextLengthW(hwnd)
    buf = win.ctypes.create_unicode_buffer(length + 1)
    win.user32.GetWindowTextW(hwnd, buf, length + 1)
    return ForegroundTarget(handle=handle, title=buf.value or "")


def _current_foreground_handle(win: Any) -> int:
    return int(win.user32.GetForegroundWindow())


def refocus_target(target: ForegroundTarget, win: Any = None) -> bool:
    """Bring ``target`` back to the foreground if it isn't already there.
    Returns whether it is the foreground window afterwards. Never raises —
    a refocus that fails just means the paste below will also fail loudly,
    which is a better error than crashing this helper."""
    _require_windows()
    win = win or _default_win()
    if _current_foreground_handle(win) == target.handle:
        return True
    try:
        win.focus_target(target.as_dict())
    except Exception:
        pass
    return _current_foreground_handle(win) == target.handle


# ── Paste / type delivery ───────────────────────────────────────────────────

_METHODS = ("clipboard", "type")


def paste_text(
    text: str,
    *,
    target: Optional[ForegroundTarget] = None,
    restore_clipboard: bool = True,
    method: str = "clipboard",
    clipboard: Any = None,
    win: Any = None,
    settle_s: float = 0.05,
) -> Dict[str, Any]:
    """Deliver ``text`` into the foreground window (or ``target`` if given
    and no longer in focus). Returns a dict describing what was done:
    ``{"method", "chars", "refocused", "clipboard_restored", "note"}``.

    ``method="clipboard"`` (default): save clipboard text, write ``text``,
    send Ctrl+V, restore the saved text. ``method="type"`` sends ``text``
    directly via SendInput Unicode events — no clipboard touched at all.
    """
    _require_windows()
    if method not in _METHODS:
        raise DictationError(f"Unknown paste method {method!r}; use 'clipboard' or 'type'")
    if not text:
        raise DictationError("No text to paste")

    win = win or _default_win()
    result: Dict[str, Any] = {
        "method": method, "chars": len(text),
        "refocused": False, "clipboard_restored": None, "note": None,
    }

    if target is not None:
        result["refocused"] = refocus_target(target, win)
        if not result["refocused"]:
            raise DictationError(
                f"Could not bring {target.title!r} back to the foreground; not pasting into whatever has focus instead."
            )

    if method == "type":
        win.type_text(text)
        return result

    clipboard = clipboard or _default_clipboard()
    previous: Optional[str] = None
    had_other_formats = False
    if restore_clipboard:
        previous = clipboard.read_text()
        try:
            had_other_formats = clipboard.has_non_text_formats()
        except Exception:
            had_other_formats = False

    clipboard.write_text(text)
    time.sleep(settle_s)
    try:
        win.key_combo(["ctrl", "v"])
    finally:
        if restore_clipboard:
            time.sleep(settle_s)
            if previous is not None:
                clipboard.write_text(previous)
                result["clipboard_restored"] = True
            else:
                result["clipboard_restored"] = False
            if had_other_formats:
                result["note"] = (
                    "The clipboard held a non-text format before dictation "
                    "(e.g. an image or rich content); only its plain text "
                    "could be preserved, so that format was lost."
                )
    return result
