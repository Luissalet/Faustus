"""Lote 28 - ACT-04: closing the window explains what it costs.

Before this change `action==='close'` in `desktop/main.cjs` closed the window
immediately, with no distinction between a window that owns its own local
server (closing it kills the server, and any turn still running on it) and a
window attached to a server shared with other Faustus windows (closing it
leaves that server, and the turn, running). The person had no way to tell
which one they were about to do — "cierre sin sorpresas" means the two must
never look the same.

`closeConfirmed()` asks via a native `dialog.showMessageBox`, using
`ownedToken` (set only when THIS instance started the server - see
server_runtime.py 'start' and src/process_ownership.py) to pick which of the
two consequences to describe, in both languages. It is skipped for secondary
windows (which never own the server), the splash screen's own cancel button
(already documented as "closes to cancel starting up"), and the automated
`--smoke-test` run, which has no dialog to click - all three preserve the
close behaviour that existed before this lote.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CJS_PATH = REPO_ROOT / "desktop" / "main.cjs"
MAIN_CJS = MAIN_CJS_PATH.read_text(encoding="utf-8")


def test_close_confirmation_exists_and_is_wired_into_the_close_action():
    assert "async function closeConfirmed(" in MAIN_CJS
    handler = MAIN_CJS.split("ipcMain.handle('faustus:window',", 1)[1]
    handler = handler.split("secureWindow(mainWindow)", 1)[0]
    assert "await closeConfirmed(target,event.senderFrame.url)" in handler, (
        "the close action must ask before it runs, not after"
    )
    # It must gate the actual close: if not confirmed, target.close() must not run.
    gate, _, rest = handler.partition("await closeConfirmed(target,event.senderFrame.url)")
    assert "return windowState(target)" in rest.split("\n", 1)[0], (
        "declining the dialog must return without closing, not fall through to target.close()"
    )


def test_the_dialog_names_ownedtoken_and_both_consequences_bilingually():
    """The whole point is telling the two situations apart, not just asking."""
    body = MAIN_CJS[MAIN_CJS.index("async function closeConfirmed"):]
    body = body[: body.index("\nfunction secureWindow")]
    assert "ownedToken" in body
    for needle in (
        "stops that server",
        "any turn still running on it stops too",
        "does not stop that server",
        "the turn keeps running there",
        "detiene", "sigue en el servidor",
    ):
        assert needle in body, f"missing consequence text: {needle!r}"


def test_declining_the_dialog_is_the_safe_default_when_the_server_is_owned():
    """Destroying your own server is the one outcome that should need an
    explicit click, never a stray Enter/Escape."""
    body = MAIN_CJS[MAIN_CJS.index("async function closeConfirmed"):]
    body = body[: body.index("\nfunction secureWindow")]
    assert re.search(r"cancelId\s*:\s*0", body)
    assert re.search(r"defaultId\s*:\s*owned\s*\?\s*0\s*:\s*1", body), (
        "when the server is owned, the default button must be Cancel (index 0)"
    )


def test_smoke_test_and_splash_and_secondary_windows_skip_the_dialog():
    """These three must keep closing exactly as before this lote: smoke.cjs
    scripts the close with nothing able to click a native dialog, and the
    splash screen's own text already promises an instant cancel."""
    body = MAIN_CJS[MAIN_CJS.index("async function closeConfirmed"):]
    guard = body[: body.index("\n  const owned")]
    assert "target!==mainWindow" in guard
    assert "senderUrl===splash" in guard
    assert "'--smoke-test'" in guard


def test_close_action_list_and_bridge_shape_are_unchanged():
    """SEC lote 16 (tests/test_sec_lote16_sec07_electron.py) pins this exact
    action list and the two-member bridge; this lote only adds a gate before
    'close' runs, it must not touch either."""
    assert re.search(
        r"\[.?'state',\s*'minimize',\s*'maximize',\s*'fullscreen',\s*'close'.?\]\.includes\(action\)",
        MAIN_CJS,
    )
    preload = (REPO_ROOT / "desktop" / "preload.cjs").read_text(encoding="utf-8")
    assert preload.count("exposeInMainWorld") == 1
