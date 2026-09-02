"""Desktop control tools (FAUSTUS): the agent sees the screen and drives it.

Seven builtin tools that act on the machine the server runs on — the owner's
Windows desktop, where Faustus runs as an interactive user process:

    desktop_screenshot     capture a monitor or a region   (READ, feeds the model an image)
    desktop_list_windows   visible top-level windows        (READ)
    desktop_focus_window   bring a window to the front      (CONTROL)
    desktop_click          mouse click at a point           (CONTROL)
    desktop_type           type text (unicode)              (CONTROL)
    desktop_key            key combo like "ctrl+s"          (CONTROL)
    desktop_scroll         mouse wheel at a point           (CONTROL)

Every handler has the ``ask_user`` shape — ``async execute(content, ctx) ->
(desc, result)`` — and NEVER raises: an unsupported platform, a headless or
locked session (black capture), a bad argument, all come back as
``{"error": ..., "exit_code": 1}`` so the model gets a sentence instead of a
traceback.

Coordinates.  A screenshot is downscaled to ``agent_tool_image_max_px`` before
it reaches the model, so the pixels the model reasons about are NOT screen
pixels.  The capture remembers its geometry (origin + scale) in
``_last_capture``; ``desktop_click`` / ``desktop_scroll`` take coordinates in
*screenshot pixels of the last capture* by default and map them back
(``screen = origin + shot / scale``).  ``coords: "screen"`` bypasses the
mapping.  Every screenshot result states the screen size, the returned image
size and the scale so the model can reason about either frame.

Platforms.  Windows through ``ctypes`` (user32 / shcore) + Pillow's
``ImageGrab``; Linux/X11 through ``xdotool`` / ``wmctrl`` when installed (with
``mss`` or ``ImageGrab`` for capture); macOS captures through ``ImageGrab``
and drives input only when ``pyautogui`` is importable.  No new hard
dependency — ``mss`` and ``pyautogui`` are optional accelerators.

Gate.  The five CONTROL tools are ``ALWAYS_APPROVE_TOOLS``
(``src/tool_capabilities.py``): an approval card on every call unless
``desktop_control_mode`` is ``ask_task`` (normal scoped gate) or ``off``
(pruned from the offer, and refused here as well, belt and braces).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.tool_capabilities import (
    ALWAYS_APPROVE_TOOLS,
    desktop_control_mode,
)
from src.tool_images import encode_image, image_is_blank, image_max_px

logger = logging.getLogger(__name__)


DESKTOP_CONTROL_TOOLS = frozenset(ALWAYS_APPROVE_TOOLS)
DESKTOP_READ_TOOLS = frozenset({"desktop_screenshot", "desktop_list_windows"})
DESKTOP_TOOLS = DESKTOP_READ_TOOLS | DESKTOP_CONTROL_TOOLS

_BUTTONS = ("left", "right", "double", "middle")
_MAX_TYPE_CHARS = 5000
_MAX_SCROLL_NOTCHES = 100


class DesktopError(Exception):
    """A user-facing failure: the message is what the model reads."""


# ── Key names ─────────────────────────────────────────────────────────────

_KEY_ALIASES = {
    "control": "ctrl", "ctl": "ctrl",
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "super": "win", "meta": "win", "windows": "win", "cmd": "win", "command": "win",
    "option": "alt",
    "pgup": "pageup", "page_up": "pageup", "pgdn": "pagedown", "page_down": "pagedown",
    "spacebar": "space",
    "bksp": "backspace", "back": "backspace",
    "ins": "insert",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
    "printscreen": "print", "prtsc": "print",
}
_MODIFIERS = ("ctrl", "alt", "shift", "win")
_NAMED_KEYS = frozenset(
    _MODIFIERS + (
        "enter", "tab", "escape", "space", "backspace", "delete", "insert",
        "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
        "capslock", "numlock", "print", "pause", "menu",
    ) + tuple(f"f{i}" for i in range(1, 25))
)


def parse_key_combo(combo: Any) -> List[str]:
    """``"Ctrl+Shift+S"`` -> ``["ctrl", "shift", "s"]`` (canonical names).

    Modifiers first (in the order given), then exactly one key: a named key
    or a single printable character.  Raises ``DesktopError`` on anything
    else so the model gets told what was wrong.
    """
    raw = str(combo or "").strip()
    if not raw:
        raise DesktopError("desktop_key needs a `combo` like \"ctrl+s\", \"alt+tab\" or \"enter\".")
    compact = raw.replace(" ", "").lower()
    if compact == "+":
        parts = ["+"]
    elif compact.endswith("++"):
        # "ctrl++" — the literal plus key with modifiers.
        parts = [p for p in compact[:-2].split("+") if p] + ["+"]
    else:
        parts = compact.split("+")
        if any(p == "" for p in parts):
            raise DesktopError(
                f"desktop_key: combo {raw!r} has an empty key part — write it like \"ctrl+s\" or \"alt+tab\"."
            )
    keys = [_KEY_ALIASES.get(p, p) for p in parts]
    mods = [k for k in keys if k in _MODIFIERS]
    rest = [k for k in keys if k not in _MODIFIERS]
    if len(rest) != 1:
        raise DesktopError(
            f"desktop_key: combo {raw!r} must have exactly one non-modifier key "
            "(e.g. \"ctrl+shift+s\", \"alt+f4\", \"enter\")."
        )
    key = rest[0]
    if key not in _NAMED_KEYS and len(key) != 1:
        raise DesktopError(
            f"desktop_key: unknown key {key!r}. Use a single character or one of: "
            + ", ".join(sorted(k for k in _NAMED_KEYS if k not in _MODIFIERS))
        )
    return mods + [key]


# ── Backend abstraction ───────────────────────────────────────────────────

class DesktopBackend:
    """Platform access. Every method may raise ``DesktopError``."""

    name = "abstract"

    def available(self) -> Tuple[bool, str]:
        return False, "desktop control is not supported on this platform"

    def screen_size(self) -> Tuple[int, int]:
        raise DesktopError("screen size unavailable")

    def list_monitors(self) -> List[Dict[str, Any]]:
        w, h = self.screen_size()
        return [{"index": 0, "left": 0, "top": 0, "width": w, "height": h, "primary": True}]

    def grab(self, region: Tuple[int, int, int, int]):
        raise DesktopError("screen capture unavailable")

    def list_windows(self) -> List[Dict[str, Any]]:
        raise DesktopError("window listing unavailable")

    def focus_window(self, title: str) -> Dict[str, Any]:
        raise DesktopError("window focus unavailable")

    def click(self, x: int, y: int, button: str) -> None:
        raise DesktopError("mouse input unavailable")

    def type_text(self, text: str) -> None:
        raise DesktopError("keyboard input unavailable")

    def key_combo(self, keys: Sequence[str]) -> None:
        raise DesktopError("keyboard input unavailable")

    def scroll(self, x: int, y: int, dy: int) -> None:
        raise DesktopError("mouse input unavailable")


class UnsupportedBackend(DesktopBackend):
    name = "unsupported"

    def __init__(self, reason: str):
        self.reason = reason

    def available(self) -> Tuple[bool, str]:
        return False, self.reason

    def _fail(self, *_a, **_k):
        raise DesktopError(self.reason)

    screen_size = grab = list_windows = focus_window = click = type_text = key_combo = scroll = _fail  # type: ignore[assignment]


def _grab_with_pillow(region: Tuple[int, int, int, int], all_screens: bool):
    from PIL import ImageGrab

    left, top, w, h = region
    bbox = (left, top, left + w, top + h)
    try:
        if all_screens:
            return ImageGrab.grab(bbox=bbox, all_screens=True)
        return ImageGrab.grab(bbox=bbox)
    except TypeError:
        return ImageGrab.grab(bbox=bbox)


def _grab_with_mss(region: Tuple[int, int, int, int]):
    """Optional accelerator: `mss` is faster and multi-monitor aware."""
    import mss  # noqa: F401 - optional
    from PIL import Image

    left, top, w, h = region
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": w, "height": h})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def _grab(region: Tuple[int, int, int, int], all_screens: bool = True):
    try:
        return _grab_with_mss(region)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - fall back to Pillow
        logger.debug("mss capture failed, using ImageGrab: %s", exc)
    return _grab_with_pillow(region, all_screens)


# ── Windows (ctypes) ──────────────────────────────────────────────────────

class WindowsBackend(DesktopBackend):
    name = "windows"

    _dpi_done = False

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._set_dpi_awareness()

    # DPI: without this a 150 % display reports logical pixels while the
    # capture is physical pixels, so every click lands short.
    def _set_dpi_awareness(self) -> None:
        if WindowsBackend._dpi_done:
            return
        WindowsBackend._dpi_done = True
        try:
            shcore = self.ctypes.WinDLL("shcore")
            shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            self.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass

    def available(self) -> Tuple[bool, str]:
        try:
            # A service / session-0 process has no interactive window station
            # and GetForegroundWindow / GetSystemMetrics report nothing useful.
            w, h = self.screen_size()
            if w <= 0 or h <= 0:
                return False, "no interactive desktop (screen size is 0x0 — is Faustus running as a service?)"
        except Exception as exc:  # noqa: BLE001
            return False, f"desktop unavailable: {exc}"
        return True, ""

    def screen_size(self) -> Tuple[int, int]:
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        w = int(self.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
        h = int(self.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
        if w <= 0 or h <= 0:
            w = int(self.user32.GetSystemMetrics(0))
            h = int(self.user32.GetSystemMetrics(1))
        return w, h

    def list_monitors(self) -> List[Dict[str, Any]]:
        ctypes, wintypes = self.ctypes, self.wintypes
        monitors: List[Dict[str, Any]] = []

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM
        )

        def _cb(hmon, _hdc, _rect, _lparam):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if self.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                monitors.append({
                    "index": len(monitors),
                    "left": int(r.left), "top": int(r.top),
                    "width": int(r.right - r.left), "height": int(r.bottom - r.top),
                    "primary": bool(info.dwFlags & 1),
                })
            return True

        try:
            self.user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_cb), 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug("EnumDisplayMonitors failed: %s", exc)
        if not monitors:
            return super().list_monitors()
        # Primary first so `monitor: 0` is the main screen.
        monitors.sort(key=lambda m: (not m["primary"], m["left"], m["top"]))
        for i, m in enumerate(monitors):
            m["index"] = i
        return monitors

    def grab(self, region: Tuple[int, int, int, int]):
        return _grab(region, all_screens=True)

    # -- windows --
    def _window_title(self, hwnd) -> str:
        length = int(self.user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return ""
        buf = self.ctypes.create_unicode_buffer(length + 1)
        self.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    def _window_rect(self, hwnd) -> List[int]:
        rect = self.wintypes.RECT()
        self.user32.GetWindowRect(hwnd, self.ctypes.byref(rect))
        return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]

    def list_windows(self) -> List[Dict[str, Any]]:
        ctypes, wintypes = self.ctypes, self.wintypes
        EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        foreground = self.user32.GetForegroundWindow()
        out: List[Dict[str, Any]] = []

        def _cb(hwnd, _lparam):
            try:
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                title = self._window_title(hwnd)
                if not title:
                    return True
                out.append({
                    "title": title,
                    "handle": int(hwnd),
                    "rect": self._window_rect(hwnd),
                    "foreground": int(hwnd) == int(foreground),
                })
            except Exception:  # noqa: BLE001 - keep enumerating
                pass
            return True

        self.user32.EnumWindows(EnumWindowsProc(_cb), 0)
        return out

    def focus_window(self, title: str) -> Dict[str, Any]:
        needle = title.lower()
        matches = [w for w in self.list_windows() if needle in w["title"].lower()]
        if not matches:
            raise DesktopError(f"no visible window title contains {title!r}")
        target = matches[0]
        hwnd = self.wintypes.HWND(target["handle"])
        SW_RESTORE = 9
        try:
            if self.user32.IsIconic(hwnd):
                self.user32.ShowWindow(hwnd, SW_RESTORE)
            # Windows refuses SetForegroundWindow from a background process
            # unless the caller "owns" the input; a synthetic ALT press is
            # the documented workaround.
            self._send_key_vk(0x12, up=False)
            self._send_key_vk(0x12, up=True)
            ok = bool(self.user32.SetForegroundWindow(hwnd))
        except Exception as exc:  # noqa: BLE001
            raise DesktopError(f"could not focus {target['title']!r}: {exc}") from exc
        if not ok:
            raise DesktopError(f"Windows refused to bring {target['title']!r} to the foreground")
        return target

    # -- input (SendInput) --
    def _input_structs(self):
        ctypes, wintypes = self.ctypes, self.wintypes
        if getattr(self, "_INPUT", None) is not None:
            return self._INPUT, self._MOUSEINPUT, self._KEYBDINPUT

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._INPUT, self._MOUSEINPUT, self._KEYBDINPUT = INPUT, MOUSEINPUT, KEYBDINPUT
        return INPUT, MOUSEINPUT, KEYBDINPUT

    def _send(self, inputs: list) -> None:
        INPUT, _, _ = self._input_structs()
        arr = (INPUT * len(inputs))(*inputs)
        sent = self.user32.SendInput(len(inputs), arr, self.ctypes.sizeof(INPUT))
        if int(sent) != len(inputs):
            err = self.ctypes.get_last_error()
            raise DesktopError(f"SendInput delivered {sent}/{len(inputs)} events (error {err})")

    def _mouse_input(self, flags: int, data: int = 0, dx: int = 0, dy: int = 0):
        INPUT, MOUSEINPUT, _ = self._input_structs()
        inp = INPUT()
        inp.type = 0  # INPUT_MOUSE
        inp.mi = MOUSEINPUT(dx, dy, data, flags, 0, 0)
        return inp

    def _key_input(self, vk: int = 0, scan: int = 0, flags: int = 0):
        INPUT, _, KEYBDINPUT = self._input_structs()
        inp = INPUT()
        inp.type = 1  # INPUT_KEYBOARD
        inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return inp

    def _send_key_vk(self, vk: int, up: bool) -> None:
        KEYEVENTF_KEYUP = 0x0002
        self._send([self._key_input(vk=vk, flags=KEYEVENTF_KEYUP if up else 0)])

    def click(self, x: int, y: int, button: str) -> None:
        MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
        MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
        MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
        if not self.user32.SetCursorPos(int(x), int(y)):
            raise DesktopError(f"SetCursorPos({x}, {y}) failed")
        if button == "right":
            seq = [(MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP)]
        elif button == "middle":
            seq = [(MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)]
        elif button == "double":
            seq = [(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)] * 2
        else:
            seq = [(MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP)]
        events = []
        for down, up in seq:
            events.append(self._mouse_input(down))
            events.append(self._mouse_input(up))
        self._send(events)

    def scroll(self, x: int, y: int, dy: int) -> None:
        MOUSEEVENTF_WHEEL = 0x0800
        WHEEL_DELTA = 120
        self.user32.SetCursorPos(int(x), int(y))
        # dy > 0 scrolls DOWN (browser deltaY convention); Windows' wheel
        # delta is positive when the wheel rolls AWAY from the user (= up).
        amount = -int(dy) * WHEEL_DELTA
        self._send([self._mouse_input(MOUSEEVENTF_WHEEL, data=amount & 0xFFFFFFFF)])

    def type_text(self, text: str) -> None:
        KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
        VK_RETURN, VK_TAB = 0x0D, 0x09
        events = []
        for ch in text:
            if ch in ("\r",):
                continue
            if ch == "\n":
                events.append(self._key_input(vk=VK_RETURN))
                events.append(self._key_input(vk=VK_RETURN, flags=KEYEVENTF_KEYUP))
                continue
            if ch == "\t":
                events.append(self._key_input(vk=VK_TAB))
                events.append(self._key_input(vk=VK_TAB, flags=KEYEVENTF_KEYUP))
                continue
            # UTF-16 code units: astral characters (emoji) are two events each.
            for unit in _utf16_units(ch):
                events.append(self._key_input(scan=unit, flags=KEYEVENTF_UNICODE))
                events.append(self._key_input(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        # SendInput in chunks: very long arrays are rejected on some builds.
        for i in range(0, len(events), 200):
            self._send(events[i:i + 200])

    _VK = {
        "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
        "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
        "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21,
        "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "capslock": 0x14, "numlock": 0x90, "print": 0x2C, "pause": 0x13, "menu": 0x5D,
        **{f"f{i}": 0x6F + i for i in range(1, 25)},
    }

    def _vk_for(self, key: str) -> int:
        if key in self._VK:
            return self._VK[key]
        self.user32.VkKeyScanW.restype = self.ctypes.c_short
        code = int(self.user32.VkKeyScanW(self.ctypes.c_wchar(key)))
        if code == -1:
            raise DesktopError(f"no virtual key for {key!r} on this keyboard layout")
        return code & 0xFF

    def key_combo(self, keys: Sequence[str]) -> None:
        KEYEVENTF_KEYUP = 0x0002
        vks = [self._vk_for(k) for k in keys]
        events = [self._key_input(vk=vk) for vk in vks]
        events += [self._key_input(vk=vk, flags=KEYEVENTF_KEYUP) for vk in reversed(vks)]
        self._send(events)


def _utf16_units(ch: str) -> List[int]:
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


# ── Linux / X11 (xdotool, wmctrl) ─────────────────────────────────────────

class LinuxBackend(DesktopBackend):
    name = "linux"

    def __init__(self):
        self.xdotool = shutil.which("xdotool")
        self.wmctrl = shutil.which("wmctrl")

    def available(self) -> Tuple[bool, str]:
        # Capture needs only a display; input additionally needs xdotool, which
        # each input method checks so a box without it can still screenshot.
        if not os.environ.get("DISPLAY"):
            return False, "no X display (DISPLAY is not set — headless session or Wayland without XWayland)"
        return True, ""

    def _need_xdotool(self) -> str:
        if not self.xdotool:
            raise DesktopError("xdotool is not installed (apt install xdotool) — needed for desktop input on Linux")
        return self.xdotool

    def _run(self, *args: str, timeout: float = 10.0) -> str:
        try:
            proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DesktopError(f"{args[0]} failed: {exc}") from exc
        if proc.returncode != 0:
            raise DesktopError(f"{' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
        return proc.stdout

    def screen_size(self) -> Tuple[int, int]:
        if self.xdotool:
            out = self._run(self.xdotool, "getdisplaygeometry").split()
            if len(out) >= 2:
                return int(out[0]), int(out[1])
        from PIL import ImageGrab

        return ImageGrab.grab().size

    def grab(self, region: Tuple[int, int, int, int]):
        return _grab(region, all_screens=False)

    def list_windows(self) -> List[Dict[str, Any]]:
        xdotool = self._need_xdotool()
        active = ""
        try:
            active = self._run(xdotool, "getactivewindow").strip()
        except DesktopError:
            pass
        out: List[Dict[str, Any]] = []
        if self.wmctrl:
            for line in self._run(self.wmctrl, "-lG").splitlines():
                parts = line.split(None, 7)
                if len(parts) < 8:
                    continue
                wid, _desk, x, y, w, h, _host, title = parts
                try:
                    handle = int(wid, 16)
                    out.append({
                        "title": title.strip(), "handle": handle,
                        "rect": [int(x), int(y), int(x) + int(w), int(y) + int(h)],
                        "foreground": bool(active) and int(active) == handle,
                    })
                except ValueError:
                    continue
            return out
        ids = self._run(xdotool, "search", "--onlyvisible", "--name", "").split()
        for wid in ids:
            try:
                title = self._run(xdotool, "getwindowname", wid).strip()
            except DesktopError:
                continue
            if not title:
                continue
            out.append({"title": title, "handle": int(wid), "rect": [], "foreground": active == wid})
        return out

    def focus_window(self, title: str) -> Dict[str, Any]:
        needle = title.lower()
        matches = [w for w in self.list_windows() if needle in w["title"].lower()]
        if not matches:
            raise DesktopError(f"no visible window title contains {title!r}")
        target = matches[0]
        self._run(self._need_xdotool(), "windowactivate", "--sync", str(target["handle"]))
        return target

    def click(self, x: int, y: int, button: str) -> None:
        code = {"left": "1", "middle": "2", "right": "3", "double": "1"}[button]
        args = [self._need_xdotool(), "mousemove", str(x), str(y), "click"]
        if button == "double":
            args += ["--repeat", "2", "--delay", "80"]
        args.append(code)
        self._run(*args)

    def type_text(self, text: str) -> None:
        self._run(self._need_xdotool(), "type", "--delay", "12", "--", text, timeout=60)

    _X_KEYS = {"ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "super", "enter": "Return",
               "tab": "Tab", "escape": "Escape", "space": "space", "backspace": "BackSpace",
               "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End",
               "pageup": "Prior", "pagedown": "Next", "up": "Up", "down": "Down", "left": "Left",
               "right": "Right", "capslock": "Caps_Lock", "numlock": "Num_Lock", "print": "Print",
               "pause": "Pause", "menu": "Menu", "+": "plus"}

    def key_combo(self, keys: Sequence[str]) -> None:
        combo = "+".join(self._X_KEYS.get(k, k.upper() if k.startswith("f") and k[1:].isdigit() else k) for k in keys)
        self._run(self._need_xdotool(), "key", "--", combo)

    def scroll(self, x: int, y: int, dy: int) -> None:
        button = "5" if dy > 0 else "4"
        self._run(self._need_xdotool(), "mousemove", str(x), str(y), "click", "--repeat", str(abs(int(dy))), "--delay", "30", button)


# ── macOS (ImageGrab + optional pyautogui) ────────────────────────────────

class MacBackend(DesktopBackend):
    name = "macos"

    def __init__(self):
        try:
            import pyautogui  # noqa: F401 - optional
            self.pyautogui = pyautogui
        except Exception:  # noqa: BLE001
            self.pyautogui = None

    def available(self) -> Tuple[bool, str]:
        return True, ""

    def _need_input(self):
        if self.pyautogui is None:
            raise DesktopError("desktop input on macOS needs the optional `pyautogui` package (pip install pyautogui)")
        return self.pyautogui

    def screen_size(self) -> Tuple[int, int]:
        if self.pyautogui is not None:
            w, h = self.pyautogui.size()
            return int(w), int(h)
        from PIL import ImageGrab

        return ImageGrab.grab().size

    def grab(self, region: Tuple[int, int, int, int]):
        return _grab(region, all_screens=False)

    def list_windows(self) -> List[Dict[str, Any]]:
        raise DesktopError("window listing is not supported on macOS yet")

    def focus_window(self, title: str) -> Dict[str, Any]:
        raise DesktopError("window focus is not supported on macOS yet")

    def click(self, x: int, y: int, button: str) -> None:
        pg = self._need_input()
        if button == "double":
            pg.doubleClick(x, y)
        else:
            pg.click(x, y, button=button)

    def type_text(self, text: str) -> None:
        self._need_input().write(text, interval=0.01)

    def key_combo(self, keys: Sequence[str]) -> None:
        pg = self._need_input()
        mapped = [{"win": "command", "escape": "esc", "pageup": "pgup", "pagedown": "pgdn"}.get(k, k) for k in keys]
        pg.hotkey(*mapped)

    def scroll(self, x: int, y: int, dy: int) -> None:
        self._need_input().scroll(-int(dy), x=x, y=y)


# ── Backend selection ─────────────────────────────────────────────────────

_backend: Optional[DesktopBackend] = None
_backend_lock = threading.Lock()


def _make_backend() -> DesktopBackend:
    system = platform.system()
    try:
        if system == "Windows" or sys.platform.startswith("win"):
            return WindowsBackend()
        if system == "Linux":
            return LinuxBackend()
        if system == "Darwin":
            return MacBackend()
    except Exception as exc:  # noqa: BLE001 - never let backend setup raise
        return UnsupportedBackend(f"desktop backend failed to initialise on {system}: {exc}")
    return UnsupportedBackend(f"desktop control is not supported on {system or 'this platform'}")


def get_backend() -> DesktopBackend:
    """The process-wide platform backend (tests monkeypatch this)."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = _make_backend()
        return _backend


def desktop_availability() -> Tuple[bool, str]:
    """Cheap, structural: can desktop tools work in this process at all?
    Used by the tool preflight to keep impossible tools off the offer."""
    try:
        return get_backend().available()
    except Exception as exc:  # noqa: BLE001
        return False, f"desktop backend unavailable: {exc}"


# ── Capture geometry (screenshot pixels -> screen pixels) ─────────────────

_capture_lock = threading.Lock()
_last_capture: Dict[str, Any] = {}


def reset_capture_state() -> None:
    with _capture_lock:
        _last_capture.clear()


def _remember_capture(origin: Tuple[int, int], scale: float, image_size: Tuple[int, int],
                      screen_size: Tuple[int, int]) -> None:
    with _capture_lock:
        _last_capture.clear()
        _last_capture.update({
            "origin": (int(origin[0]), int(origin[1])),
            "scale": float(scale),
            "image_size": (int(image_size[0]), int(image_size[1])),
            "screen_size": (int(screen_size[0]), int(screen_size[1])),
        })


def map_to_screen(x: float, y: float, coords: str) -> Tuple[int, int, str]:
    """Return ``(screen_x, screen_y, frame_note)`` for a tool coordinate.

    ``coords="screenshot"`` (default) maps through the last capture's origin
    and scale; with no capture yet it is the screen frame.  ``"screen"`` is
    taken as-is.
    """
    with _capture_lock:
        capture = dict(_last_capture)
    if coords == "screen" or not capture:
        return int(round(x)), int(round(y)), "screen pixels"
    ox, oy = capture["origin"]
    scale = capture["scale"] or 1.0
    sx = int(round(ox + x / scale))
    sy = int(round(oy + y / scale))
    return sx, sy, f"screenshot pixels (scale {scale:.4g}, origin {ox},{oy}) -> screen"


# ── Argument helpers ──────────────────────────────────────────────────────

def _parse_args(content: Any) -> Dict[str, Any]:
    raw = (content or "").strip() if isinstance(content, str) else content
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": str(raw)}
    return parsed if isinstance(parsed, dict) else {"_raw": parsed}


def _int_arg(args: Dict[str, Any], key: str, *, required: bool = True, default: Optional[int] = None) -> Optional[int]:
    value = args.get(key, None)
    if value is None or value == "":
        if required:
            raise DesktopError(f"missing integer argument `{key}`")
        return default
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise DesktopError(f"argument `{key}` must be a number, got {value!r}") from None


def _error(tool: str, message: str) -> Tuple[str, Dict[str, Any]]:
    return f"{tool}: failed", {"error": f"{tool}: {message}", "exit_code": 1}


def _control_mode_guard(tool: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    if tool in DESKTOP_CONTROL_TOOLS and desktop_control_mode() == "off":
        return _error(tool, "desktop control is disabled (setting desktop_control_mode=off). "
                            "Do not call desktop input tools again in this turn.")
    return None


def _primary_monitor_centre(backend: DesktopBackend) -> Tuple[int, int]:
    """Centre of the primary monitor (the first listed one when none is
    flagged). ``screen_size() // 2`` was the centre of the VIRTUAL screen
    without its origin: with a second monitor left of the primary the virtual
    screen starts at x=-1920, so the "centre" landed on the primary's edge or,
    with the primary on the right, outside every monitor."""
    try:
        monitors = [m for m in backend.list_monitors() if int(m.get("width", 0)) > 0 and int(m.get("height", 0)) > 0]
    except Exception:  # noqa: BLE001 - no monitor list: fall back to the plain size
        monitors = []
    if monitors:
        primary = next((m for m in monitors if m.get("primary")), monitors[0])
        return (int(primary["left"]) + int(primary["width"]) // 2,
                int(primary["top"]) + int(primary["height"]) // 2)
    screen_w, screen_h = backend.screen_size()
    return screen_w // 2, screen_h // 2


def _check_on_screen(backend: DesktopBackend, x: int, y: int) -> None:
    try:
        monitors = backend.list_monitors()
    except Exception:  # noqa: BLE001 - cannot verify, let the platform decide
        return
    for m in monitors:
        if m["left"] <= x < m["left"] + m["width"] and m["top"] <= y < m["top"] + m["height"]:
            return
    geometry = "; ".join(
        "{}x{} at {},{}".format(m["width"], m["height"], m["left"], m["top"]) for m in monitors
    )
    raise DesktopError(
        f"point ({x}, {y}) is outside every monitor ({geometry}). "
        "Take a fresh desktop_screenshot and use its pixel coordinates."
    )


# ── Handlers ──────────────────────────────────────────────────────────────

class DesktopTool:
    """One handler per tool name, all with the ``ask_user`` signature."""

    def __init__(self, name: str):
        if name not in DESKTOP_TOOLS:
            raise ValueError(f"unknown desktop tool {name!r}")
        self.name = name

    async def execute(self, content, ctx):
        try:
            return await self._execute(content, ctx)
        except DesktopError as exc:
            return _error(self.name, str(exc))
        except Exception as exc:  # noqa: BLE001 - tools never raise
            logger.warning("%s failed: %s", self.name, exc, exc_info=True)
            return _error(self.name, f"{type(exc).__name__}: {exc}")

    async def _execute(self, content, ctx):
        args = _parse_args(content)
        guard = _control_mode_guard(self.name)
        if guard is not None:
            return guard
        backend = get_backend()
        ok, reason = backend.available()
        if not ok:
            raise DesktopError(reason)
        handler = getattr(self, "_" + self.name[len("desktop_"):])
        return handler(args, backend)

    # -- desktop_screenshot --
    def _screenshot(self, args: Dict[str, Any], backend: DesktopBackend):
        monitors = backend.list_monitors()
        screen_w, screen_h = backend.screen_size()
        monitor_index = _int_arg(args, "monitor", required=False, default=0) or 0
        region_arg = args.get("region")
        if region_arg not in (None, "", []):
            if not isinstance(region_arg, (list, tuple)) or len(region_arg) != 4:
                raise DesktopError("`region` must be [x, y, width, height] in screen pixels")
            try:
                rx, ry, rw, rh = (int(round(float(v))) for v in region_arg)
            except (TypeError, ValueError):
                raise DesktopError("`region` must be four numbers [x, y, width, height]") from None
            if rw <= 0 or rh <= 0:
                raise DesktopError("`region` width and height must be positive")
            region = (rx, ry, rw, rh)
            label = f"region {rx},{ry} {rw}x{rh}"
        else:
            match = next((m for m in monitors if m.get("index") == monitor_index), None)
            if match is None:
                raise DesktopError(
                    f"monitor {monitor_index} does not exist; available: "
                    + ", ".join(f"{m['index']} ({m['width']}x{m['height']})" for m in monitors)
                )
            region = (match["left"], match["top"], match["width"], match["height"])
            label = f"monitor {monitor_index}"

        img = backend.grab(region)
        if img is None:
            raise DesktopError("screen capture returned nothing")
        if image_is_blank(img):
            raise DesktopError(
                "the capture is blank (uniform colour) — the session is probably locked, "
                "headless, or Faustus is not running on the interactive desktop"
            )
        b64, mime, info = encode_image(img, max_px=image_max_px())
        scale = float(info["scale"])
        _remember_capture((region[0], region[1]), scale, (info["width"], info["height"]), (screen_w, screen_h))

        src_w, src_h = info["source_width"], info["source_height"]
        lines = [
            f"Screenshot of {label}: captured {src_w}x{src_h} px, returned as {info['width']}x{info['height']} px "
            f"(scale {scale:.4g}). Screen (all monitors): {screen_w}x{screen_h} px.",
            "Coordinates for desktop_click / desktop_scroll: give x,y in pixels of THIS image "
            "(default coords=\"screenshot\"); they are mapped back to the screen automatically. "
            "Use coords=\"screen\" for raw screen pixels.",
        ]
        if len(monitors) > 1:
            lines.append("Monitors: " + "; ".join(
                f"{m['index']}: {m['width']}x{m['height']} at {m['left']},{m['top']}{' (primary)' if m.get('primary') else ''}"
                for m in monitors))
        result: Dict[str, Any] = {
            "output": "\n".join(lines),
            "exit_code": 0,
            "images": [{"data": b64, "mimeType": mime}],
            "screenshot": f"data:{mime};base64,{b64}",
            "screen": {"width": screen_w, "height": screen_h},
            "image": {"width": info["width"], "height": info["height"]},
            "scale": scale,
            "monitor": monitor_index,
        }
        if region_arg not in (None, "", []):
            result["region"] = list(region)
        return f"desktop_screenshot: {label}", result

    # -- desktop_list_windows --
    def _list_windows(self, args: Dict[str, Any], backend: DesktopBackend):
        windows = backend.list_windows()
        if not windows:
            return "desktop_list_windows: none", {"output": "No visible windows.", "exit_code": 0, "windows": []}
        lines = []
        for w in windows:
            rect = w.get("rect") or []
            geo = f" [{rect[0]},{rect[1]} {rect[2] - rect[0]}x{rect[3] - rect[1]}]" if len(rect) == 4 else ""
            mark = " * (foreground)" if w.get("foreground") else ""
            lines.append(f"- {w['title']}{geo}{mark}")
        return f"desktop_list_windows: {len(windows)} window(s)", {
            "output": f"{len(windows)} visible window(s):\n" + "\n".join(lines),
            "exit_code": 0,
            "windows": windows,
        }

    # -- desktop_focus_window --
    def _focus_window(self, args: Dict[str, Any], backend: DesktopBackend):
        title = str(args.get("title") or args.get("_raw") or "").strip()
        if not title:
            raise DesktopError("missing `title` (a substring of the window title)")
        target = backend.focus_window(title)
        return f"desktop_focus_window: {target['title'][:60]}", {
            "output": f"Focused window: {target['title']}",
            "exit_code": 0,
            "window": target,
        }

    # -- desktop_click --
    def _click(self, args: Dict[str, Any], backend: DesktopBackend):
        x = _int_arg(args, "x")
        y = _int_arg(args, "y")
        button = str(args.get("button") or "left").strip().lower()
        if button not in _BUTTONS:
            raise DesktopError(f"`button` must be one of {', '.join(_BUTTONS)}; got {button!r}")
        coords = str(args.get("coords") or "screenshot").strip().lower()
        sx, sy, note = map_to_screen(x, y, coords)
        _check_on_screen(backend, sx, sy)
        backend.click(sx, sy, button)
        return f"desktop_click: {button} at {sx},{sy}", {
            "output": f"{button} click at screen ({sx}, {sy}) [{note}]. Take a new desktop_screenshot to see the effect.",
            "exit_code": 0,
            "screen_xy": [sx, sy],
            "button": button,
        }

    # -- desktop_type --
    def _type(self, args: Dict[str, Any], backend: DesktopBackend):
        text = args.get("text")
        if text is None:
            text = args.get("_raw")
        text = "" if text is None else str(text)
        if not text:
            raise DesktopError("missing `text` to type")
        if len(text) > _MAX_TYPE_CHARS:
            raise DesktopError(f"`text` is longer than {_MAX_TYPE_CHARS} characters; split it")
        backend.type_text(text)
        return f"desktop_type: {len(text)} chars", {
            "output": f"Typed {len(text)} character(s) into the focused window.",
            "exit_code": 0,
        }

    # -- desktop_key --
    def _key(self, args: Dict[str, Any], backend: DesktopBackend):
        combo = args.get("combo") or args.get("keys") or args.get("key") or args.get("_raw")
        keys = parse_key_combo(combo)
        backend.key_combo(keys)
        return f"desktop_key: {'+'.join(keys)}", {
            "output": f"Pressed {'+'.join(keys)}.",
            "exit_code": 0,
            "keys": keys,
        }

    # -- desktop_scroll --
    def _scroll(self, args: Dict[str, Any], backend: DesktopBackend):
        dy = _int_arg(args, "dy")
        if dy == 0:
            raise DesktopError("`dy` must be a non-zero number of wheel notches (positive = down)")
        if abs(dy) > _MAX_SCROLL_NOTCHES:
            raise DesktopError(f"`dy` is capped at {_MAX_SCROLL_NOTCHES} notches per call")
        x = _int_arg(args, "x", required=False)
        y = _int_arg(args, "y", required=False)
        coords = str(args.get("coords") or "screenshot").strip().lower()
        if x is None or y is None:
            sx, sy = _primary_monitor_centre(backend)
            note = "primary monitor centre"
        else:
            sx, sy, note = map_to_screen(x, y, coords)
        _check_on_screen(backend, sx, sy)
        backend.scroll(sx, sy, dy)
        direction = "down" if dy > 0 else "up"
        return f"desktop_scroll: {direction} {abs(dy)} at {sx},{sy}", {
            "output": f"Scrolled {direction} {abs(dy)} notch(es) at screen ({sx}, {sy}) [{note}].",
            "exit_code": 0,
            "screen_xy": [sx, sy],
        }


DESKTOP_TOOL_HANDLERS = {name: DesktopTool(name).execute for name in sorted(DESKTOP_TOOLS)}
