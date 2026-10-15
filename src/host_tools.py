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
