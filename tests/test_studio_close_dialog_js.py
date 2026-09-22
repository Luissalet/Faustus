"""Closing the window: what it decides, now that it no longer asks.

SINCE 22-09-2026: the dialog described below is GONE. `closeConfirmed()`
says so in its own first line -- "No questions, ever (the owner's rule)":
the X parks the window in the tray, and Quit from the tray is what closes
it and stops a server this window started. Five tests here went on pinning
that dialog's wording, buttons and Escape default long after it was
removed; they were rewritten to check the decision the function actually
makes. The history below is kept because it explains WHY the decision
still distinguishes an owned server from a shared one, which is the part
that survived.

Lote 28 - ACT-04 (historical): closing the window explains what it costs.

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


def test_closing_never_asks():
    """The owner's rule, written into `closeConfirmed` itself: "No
    questions, ever". The X parks the window in the tray; Quit from the
    tray is what closes it.

    Five tests here used to pin the wording, the buttons and the Escape
    default of a native dialog that this function no longer opens. They had
    been red ever since it was removed, describing a product that is not
    the one that ships. What replaced them is the decision the function
    actually makes.
    """
    body = _close_confirmed_body()
    assert "showMessageBox" not in body
    assert "dialog." not in body


def test_an_owned_server_stops_and_a_shared_one_is_left_alone():
    """`ownedToken` is set only when THIS instance started the server, so
    it is the whole difference between killing a server and walking away
    from one somebody else is on."""
    body = _close_confirmed_body()
    assert "return ownedToken?'stop':'close'" in body


def test_secondary_windows_the_splash_and_the_smoke_run_just_close():
    """These three never own the server, and the smoke run has nobody to
    click anything. They resolve to a plain close before `ownedToken` is
    even consulted."""
    body = _close_confirmed_body()
    guard = body[: body.index("return ownedToken")]
    assert "target!==mainWindow" in guard
    assert "senderUrl===splash" in guard
    assert "'--smoke-test'" in guard
    assert "return 'close'" in guard


def test_every_decision_is_one_the_handler_knows():
    """The handler branches on 'cancel', 'keep' and 'stop', and treats
    anything else as a plain close. Nothing returns 'cancel' or 'keep'
    today, which is fine -- but a fourth word would be swallowed in
    silence, so pin the vocabulary rather than the count."""
    words = set(re.findall(r"'(cancel|keep|stop|close|[a-z]+)'", _close_confirmed_body()))
    unexpected = words - {"cancel", "keep", "stop", "close", "question"}
    assert not unexpected, f"closeConfirmed can return something the handler does not know: {unexpected}"

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
