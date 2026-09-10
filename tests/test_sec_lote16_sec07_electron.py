"""Lote 16 (Seguridad) - SEC-07: Electron and active content.

QA-32 (tests/qa/test_qa_32_preview_malicioso.py) already proves every
BrowserWindow disables node integration/enables the sandbox, and that the
three HTML preview surfaces render inside a sandboxed iframe without
`allow-same-origin` + `allow-scripts` together. This file adds what the lot
brief asks for beyond that: the preload bridge itself exposes no generic
filesystem/shell/IPC primitive a malicious preview could reach if it ever
broke out of its iframe, and every CSP declared in desktop/main.cjs forbids
`unsafe-eval` - the one directive that would let injected HTML run arbitrary
strings as code even inside a locked-down renderer.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_CJS = (REPO_ROOT / "desktop" / "main.cjs").read_text(encoding="utf-8")
PRELOAD_CJS = (REPO_ROOT / "desktop" / "preload.cjs").read_text(encoding="utf-8")


def test_no_csp_in_the_desktop_shell_allows_unsafe_eval():
    """`unsafe-eval` would let any script that does run (even one confined by
    sandbox/CSP script-src otherwise) eval() arbitrary strings - the one gap
    that turns "no code injection" into "code injection", so it must never
    appear in any Content-Security-Policy this shell serves itself."""
    csps = re.findall(r'Content-Security-Policy"\s*content="([^"]*)"', MAIN_CJS)
    assert csps, "expected at least one Content-Security-Policy in desktop/main.cjs"
    for csp in csps:
        assert "unsafe-eval" not in csp, f"CSP allows unsafe-eval: {csp!r}"


def test_preload_bridge_exposes_no_filesystem_shell_or_generic_ipc_primitive():
    """The bridge (`contextBridge.exposeInMainWorld`) is the only door from a
    loaded page into anything privileged. It must name window-chrome actions
    only ('faustus:window' invoke, a window-state subscription) - never a
    generic ipcRenderer.send/invoke passthrough, `require`, `fs`, `shell`, or
    `process` a malicious HTML/SVG artifact preview could ride out through,
    should it ever escape its own sandboxed iframe."""
    assert "contextBridge.exposeInMainWorld" in PRELOAD_CJS
    exposed = re.search(
        r"exposeInMainWorld\('faustusWindow',\s*Object\.freeze\((\{.*?\})\)\)",
        PRELOAD_CJS, re.DOTALL,
    )
    assert exposed, "expected a single frozen faustusWindow bridge object"
    bridge_body = exposed.group(1)

    # Only the two documented members - a command invoker restricted to a
    # single fixed channel, and a state-change subscription.
    assert re.search(r"command\s*:\s*action\s*=>\s*ipcRenderer\.invoke\('faustus:window',\s*action\)", bridge_body)
    assert "ipcRenderer.on" in bridge_body

    forbidden_tokens = (
        "require(", "process.", "fs.", "child_process", "exec(",
        "shell.openPath", "shell.showItemInFolder", "readFile", "writeFile",
    )
    for token in forbidden_tokens:
        assert token not in bridge_body, f"preload bridge exposes {token!r}"

    # The dangerous middle ground: a *property* whose value is the whole
    # `ipcRenderer` object handed straight to the page, rather than one fixed
    # invoke() call to one fixed channel - that would let any loaded page
    # send/invoke on ANY IPC channel Faustus's main process has ever wired up.
    assert not re.search(r":\s*ipcRenderer\s*[,}]", bridge_body), (
        "preload bridge exposes the raw ipcRenderer object, not a scoped call"
    )
    # Exactly the two documented top-level members - `command` and
    # `subscribe` - so a third one added later has to change this test.
    top_level_keys = re.findall(r"(?:\{|,)\s*(\w+)\s*:", bridge_body)
    assert set(top_level_keys) == {"command", "subscribe"}, top_level_keys

    # No second exposeInMainWorld call hiding a wider surface elsewhere in
    # the file, and no bare ipcRenderer handed to the page unwrapped.
    assert PRELOAD_CJS.count("exposeInMainWorld") == 1
    assert "exposeInMainWorld('ipcRenderer'" not in PRELOAD_CJS
    assert "exposeInMainWorld('electron'" not in PRELOAD_CJS


def test_the_native_ipc_handler_only_accepts_the_documented_window_actions():
    """`ipcMain.handle('faustus:window', ...)` is the one channel the preload
    bridge can reach; it must reject anything outside the fixed action list,
    so a compromised renderer cannot widen what that single channel does by
    sending an unexpected string."""
    handler = MAIN_CJS.split("ipcMain.handle('faustus:window',", 1)[1]
    handler = handler.split("secureWindow(mainWindow)", 1)[0]
    assert "Unknown window action" in handler
    assert re.search(
        r"\[.?'state',\s*'minimize',\s*'maximize',\s*'fullscreen',\s*'close'.?\]\.includes\(action\)",
        handler,
    )


def test_only_local_pages_may_invoke_the_native_window_bridge():
    """The IPC handler checks the calling frame is the window's own main
    frame AND that its URL is either the local app origin or (for the one
    'close' action needed before the server is up) the hardcoded splash
    page - never an arbitrary/remote page riding along in a subframe."""
    handler = MAIN_CJS.split("ipcMain.handle('faustus:window',", 1)[1][:600]
    assert "event.senderFrame!==target.webContents.mainFrame" in handler
    assert "localNavigation(event.senderFrame.url,origin)" in handler
    assert "Untrusted window" in handler
