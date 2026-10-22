"""Tools this host cannot run at all, so they are never offered to the model.

A Linux container or a headless server has no PowerShell and no screen. Offering
``powershell`` or ``desktop_screenshot`` there only costs a round: the model calls
it, gets "no PowerShell on this host", and tries again with something else. The
answer is the same for the whole life of the process, so it is computed once.

* ``powershell``: offered when Windows, or when ``pwsh``/``powershell`` is on PATH.
* the desktop tools: offered on Windows and macOS, and on Linux when a display
  (``DISPLAY`` or ``WAYLAND_DISPLAY``) is set.

``FAUSTUS_HOST_TOOLS_OFF`` (comma list) adds names by hand; ``FAUSTUS_HOST_TOOLS_ON``
forces names back on (an operator who knows better than the probe).
"""
from __future__ import annotations

import functools
import os
import shutil
import sys

DESKTOP_TOOLS = frozenset({
    "desktop_screenshot", "desktop_list_windows", "desktop_focus_window",
    "desktop_click", "desktop_type", "desktop_key", "desktop_scroll",
})
# The semantic desktop tools go through the same on-screen indicator.
DESKTOP_APP_TOOLS = frozenset({"desktop_snapshot", "desktop_find", "desktop_act"})


def _names(var: str) -> set[str]:
    return {n.strip() for n in os.environ.get(var, "").split(",") if n.strip()}


@functools.lru_cache(maxsize=1)
def unavailable_tools() -> frozenset[str]:
    off: set[str] = set()
    if os.name != "nt" and not (shutil.which("pwsh") or shutil.which("powershell")):
        off.add("powershell")
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        off |= DESKTOP_TOOLS
    off |= _names("FAUSTUS_HOST_TOOLS_OFF")
    off -= _names("FAUSTUS_HOST_TOOLS_ON")
    return frozenset(off)


def runnable(names):
    """``names`` without the tools this host cannot run (same type when it is a set or frozenset)."""
    off = unavailable_tools()
    if not off:
        return names
    kept = [n for n in names if n not in off]
    return type(names)(kept) if isinstance(names, (set, frozenset)) else kept


# --- the desktop app on Windows -------------------------------------------
#
# On Windows every desktop tool first shows the on-screen indicator and the
# Escape stop, and those live in the Faustus desktop app
# (desktop/desktop-control.cjs). With Faustus run from the web launcher and
# the app closed, each call waits 4 s and fails with "Open the Faustus desktop
# app" -- seen live 22-10 (session c9bb4580): the model, offered only desktop
# tools, spent 15 rounds trying. So on Windows the desktop tools are offered
# only while the app is running: its watcher writes data/runtime/desktop-app.json
# every couple of seconds; an older app build without that heartbeat is
# recognised by its process instead.

_APP_FRESH_S = 15.0
_APP_CHECK_TTL_S = 5.0
_app_cache: list = [0.0, False]


def _runtime_dir() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "runtime")


def _heartbeat_fresh(now: float) -> bool:
    import json
    try:
        with open(os.path.join(_runtime_dir(), "desktop-app.json"), encoding="utf-8") as fh:
            beat = json.load(fh)
        return now - float(beat.get("updated") or 0) < _APP_FRESH_S
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _app_process_running() -> bool:
    try:
        import psutil
    except Exception:  # noqa: BLE001 - no probe, no claim
        return False
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))).lower()
    desktop = os.path.join(root, "desktop").lower()
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            name = str(proc.info.get("name") or "").lower()
            if not name.startswith(("electron", "faustus")):
                continue
            line = " ".join(proc.info.get("cmdline") or []).lower() + " " + str(proc.info.get("exe") or "").lower()
            if desktop in line or root in line:
                return True
        except Exception:  # noqa: BLE001 - a process that went away
            continue
    return False


def desktop_app_running() -> bool:
    """True when the desktop tools can show their indicator (Windows needs the app)."""
    if os.name != "nt":
        return True
    import time
    now = time.time()
    if now - _app_cache[0] < _APP_CHECK_TTL_S:
        return bool(_app_cache[1])
    ready = _heartbeat_fresh(now) or _app_process_running()
    _app_cache[0], _app_cache[1] = now, ready
    return ready


def turn_unavailable_tools() -> frozenset[str]:
    """``unavailable_tools`` plus what cannot run right now (the desktop tools
    on Windows while the desktop app is closed)."""
    off = set(unavailable_tools())
    if not desktop_app_running():
        off |= DESKTOP_TOOLS | DESKTOP_APP_TOOLS
        off -= _names("FAUSTUS_HOST_TOOLS_ON")
    return frozenset(off)
