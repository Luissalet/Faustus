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

Lote 69a (ACT-04, same ID, still open per docs/spec/v2): the owned-server
dialog above only ever said the server would stop — it never said what that
actually interrupted, and had no way to close the window while leaving the
server (and its work) running. `closeConfirmed()` now returns one of three
decisions ('cancel' / 'keep' / 'stop') instead of a boolean, names a
best-effort count of tasks in flight (`activeTaskCount()`, GET /api/queue),
and offers a third button — "keep the server running" — that clears
`ownedToken` before the window closes so `shutdown()` skips the stop call
for that window, the same way it already does for a non-owning one.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CJS_PATH = REPO_ROOT / "desktop" / "main.cjs"
MAIN_CJS = MAIN_CJS_PATH.read_text(encoding="utf-8")


def _close_confirmed_body() -> str:
    body = MAIN_CJS[MAIN_CJS.index("async function closeConfirmed"):]
    return body[: body.index("\nfunction secureWindow")]


def test_close_confirmation_exists_and_is_wired_into_the_close_action():
    assert "async function closeConfirmed(" in MAIN_CJS
    handler = MAIN_CJS.split("ipcMain.handle('faustus:window',", 1)[1]
    handler = handler.split("secureWindow(mainWindow)", 1)[0]
    assert "closeDecision=await closeConfirmed(target,event.senderFrame.url)" in handler, (
        "the close action must ask before it runs, not after"
    )
    # It must gate the actual close: declining ('cancel') must return without
    # ever reaching target.close() below.
    gate, _, rest = handler.partition("closeDecision=await closeConfirmed(target,event.senderFrame.url)")
    assert "if(closeDecision==='cancel')return windowState(target)" in rest.split("\n", 2)[1], (
        "declining the dialog must return without closing, not fall through to target.close()"
    )


def test_keeping_the_server_clears_ownedtoken_before_the_window_closes():
    """The third option - close the window, leave the server (and whoever
    else is attached to it) running - has to act on the SAME variable
    shutdown() checks, or 'keep' would be indistinguishable from 'stop'."""
    handler = MAIN_CJS.split("ipcMain.handle('faustus:window',", 1)[1]
    handler = handler.split("secureWindow(mainWindow)", 1)[0]
    assert "if(closeDecision==='keep')ownedToken=''" in handler
    # And it must run BEFORE the window actually closes (setImmediate call).
    keep_at = handler.index("closeDecision==='keep'")
    close_at = handler.index("setImmediate(()=>{if(!target.isDestroyed())target.close();})")
    assert keep_at < close_at, "clearing ownedToken must happen before the window closes"


def test_the_dialog_names_ownedtoken_and_both_consequences_bilingually():
    """The whole point is telling the two situations (and the two islands of
    action the resulting decision splits into) apart, not just asking."""
    body = _close_confirmed_body()
    assert "ownedToken" in body
    for needle in (
        # non-owned (shared server) case
        "does not stop that server",
        "the turn keeps running there",
        "detiene",
        "sigue en el servidor",
        # owned case: names what a stop interrupts, in both languages
        "task", "tarea",
        "in progress", "en marcha",
        "Keep the server running",
        "Mantener el servidor",
    ):
        assert needle in body, f"missing consequence text: {needle!r}"


def test_owned_dialog_offers_keep_and_stop_as_distinct_choices():
    body = _close_confirmed_body()
    assert "Keep the server running / Mantener el servidor" in body
    assert "Close and stop the server / Cerrar y detener el servidor" in body
    assert "response===2?'stop':response===1?'keep':'cancel'" in body


def test_declining_the_dialog_is_the_safe_default_when_the_server_is_owned():
    """Destroying your own server (or, now, silently detaching from it) is
    the one outcome that should need an explicit click, never a stray
    Enter/Escape - both default to Cancel."""
    body = _close_confirmed_body()
    assert body.count("cancelId:0") == 2, "both the owned and non-owned dialogs must cancel by default on Escape"
    # The owned dialog (three buttons) defaults its Enter/primary action to Cancel too.
    owned_dialog = body[body.index("const count=await activeTaskCount()"):]
    assert re.search(r"defaultId\s*:\s*0\s*,\s*\n\s*cancelId\s*:\s*0", owned_dialog), (
        "when the server is owned, the default button must be Cancel (index 0)"
    )


def test_best_effort_task_count_degrades_to_unknown_rather_than_blocking():
    """A failed /api/queue read must not stop the dialog from opening, and
    must not be misreported as "nothing in progress"."""
    assert "async function activeTaskCount(" in MAIN_CJS
    fn = MAIN_CJS[MAIN_CJS.index("async function activeTaskCount("):]
    fn = fn[: fn.index("\nasync function closeConfirmed")]
    assert "/api/queue" in fn
    assert "catch{return null;}" in fn
    body = _close_confirmed_body()
    assert "count===null" in body, "an unknown count must be its own branch, not folded into count>0/count===0"


def test_smoke_test_and_splash_and_secondary_windows_skip_the_dialog():
    """These three must keep closing exactly as before this lote: smoke.cjs
    scripts the close with nothing able to click a native dialog, and the
    splash screen's own text already promises an instant cancel."""
    body = _close_confirmed_body()
    guard = body[: body.index("\n  const owned")]
    assert "target!==mainWindow" in guard
    assert "senderUrl===splash" in guard
    assert "'--smoke-test'" in guard
    assert "return 'close'" in guard, "the skip path must still resolve to a close decision, not a bare true"


def test_close_action_list_and_bridge_shape_are_unchanged():
    """SEC lote 16 (tests/test_sec_lote16_sec07_electron.py) pins this exact
    action list and the two-member bridge; this lote only changes what
    closeConfirmed() decides, it must not touch either."""
    assert re.search(
        r"\[.?'state',\s*'minimize',\s*'maximize',\s*'fullscreen',\s*'close'.?\]\.includes\(action\)",
        MAIN_CJS,
    )
    preload = (REPO_ROOT / "desktop" / "preload.cjs").read_text(encoding="utf-8")
    assert preload.count("exposeInMainWorld") == 1
