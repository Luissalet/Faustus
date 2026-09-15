"""Relaunching the desktop app must not throw on a dead window.

Electron keeps the JavaScript BrowserWindow after the native window is
gone. `app.on('second-instance')` used to call isMinimized() on that
object, which throws `TypeError: Object has been destroyed` — the dialog
that pops up when Start-Faustus-Desktop.bat is run while a previous
window has already died (crash, freeze, or shutdown still draining).
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CJS = (REPO_ROOT / "desktop" / "main.cjs").read_text(encoding="utf-8")


def _second_instance_handler() -> str:
    marker = "app.on('second-instance',"
    start = MAIN_CJS.index(marker)
    return MAIN_CJS[start : MAIN_CJS.index("});", start) + 3]


def test_second_instance_does_not_touch_a_destroyed_window():
    assert "const alive=w=>w&&!w.isDestroyed();" in MAIN_CJS
    handler = _second_instance_handler()
    assert "alive(mainWindow)" in handler
    alive_at = handler.index("alive(mainWindow)")
    assert handler.index("isMinimized()") > alive_at
    assert handler.index(".show()") > alive_at
    assert handler.index(".focus()") > alive_at


def test_permission_handlers_do_not_call_geturl_on_a_destroyed_contents():
    assert "const liveContents=c=>c&&!c.isDestroyed();" in MAIN_CJS
    check = MAIN_CJS.split("setPermissionCheckHandler(", 1)[1]
    check = check.split("setPermissionRequestHandler(", 1)[0]
    assert "liveContents(contents)" in check
    request = MAIN_CJS.split("setPermissionRequestHandler(", 1)[1]
    request = request.split("mainWindow=new BrowserWindow", 1)[0]
    assert "liveContents(contents)" in request
    assert "alive(mainWindow)" in request
