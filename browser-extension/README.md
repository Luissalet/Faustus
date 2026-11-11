# Faustus Browser Session

Own Manifest V3 extension for the browser session in which it is installed. It connects only to `ws://127.0.0.1:7016`, identifies as `cneonpjikbdhbaddfpmmdjemfdphlppa` through the fixed public manifest key, and uses `chrome.debugger` on an explicit tab. Connecting starts no server, tab, browser or profile. No external scripts or dependencies.

`tabs` lists metadata without activating anything. `snapshot` reads main-frame Accessibility roles, names, values and properties with references scoped to this connection, tab, document and snapshot generation. Cross-frame targeting is not implemented. New snapshots, navigation, detach and reconnection invalidate references. DOM references are resolved with backend DOM ids; the original eight actions do not evaluate page JavaScript or framework callbacks. The added `download` uses only fixed product readers for an observed link/Blob; it never accepts caller code or invokes Writer internals.

`click` uses visible target content quads and verifies the actual DOM hit in the same main frame before targeted CDP mouse input. The integer CSS point is identical for hit-test and input. The hit must be the referenced node or one of its descendants, including light/shadow DOM, pseudo-elements and distributed children. A covering node, foreign frame or missing hit returns `target_obscured` before mouse input: observe and take a fresh snapshot. It never chooses another target or retries automatically. `type` **inserts Unicode text at the focused selection**, without clearing/replacing the field. To replace explicitly, use `press` with `Ctrl+A` and then `type`, each against the newly returned snapshot. `press` supports Enter, Escape, Tab, Backspace, Delete, arrows, Home/End/PageUp/PageDown, F1–F12, ASCII letters/digits, Space and Ctrl/Control/Alt/Shift/Meta/Cmd modifiers. Its optional ref focuses that node; otherwise it uses the focus within the explicit tab. `navigate` accepts http(s). Each mutation attempts one fresh AX readback.

Successful mutations return `phase: readback_complete`; a browser ACK followed by readback failure is an error with `phase: action_confirmed`. Failure after dispatch without full ACK is `outcome_unknown`. This classification does not assert that the website completed the requested application operation. There is no automatic retry or replay, including after reconnect. Observe the page before deciding whether an ambiguous action should be attempted again.

`screenshot` uses `Page.captureScreenshot` on the requested tab, not the visible tab of a window. `release` detaches only a debugger attachment made by this extension; it closes nothing. Failure to attach while another debugger owns a target does not evict it. Agent operations never activate a tab or window or send desktop input. The person's explicit **Ver pestaña** button in the extension panel activates the chosen tab.

The badge reports relay availability. Clicking it opens the session panel, with **Reconectar**, human-turn cards and tab adoption. Reconnection does not replay a command. A20second ping keeps an active WebSocket/service worker alive on Chromium116+, with a one-minute reconnect alarm. Disconnect invalidates refs and releases owned debugger attachments, including an attach completing after disconnect. No cookies or command history are stored and there is no profile isolation claim.

Node tests use a simulated Chrome/CDP/WebSocket environment. Separately, the user installed this extension in Brave and Root exercised all eight actions over real MCP, including background input, a tab-specific screenshot, Library and return Home. The active tab and five visible QA projects were preserved. This is evidence from an MCP client; autonomous use by Faustus's model and portable runtime support remain pending. The Python backend provides compact readbacks by default and preserves the original full representation with `detail: full`; the extension's wire graph is unchanged.


## File actions (implementation under review; real-browser acceptance pending)

`upload` takes the current `tab_id`/`ref` and absolute local `files`. It intercepts that target's file chooser before normal targeted input, sets its file input and restores interception. Its receipt proves selection, not that the application imported the file. The input may immediately clear its own value.

`download` takes a current anchor ref and a new absolute `output_path`. Fixed `Runtime.callFunctionOn` readers fetch that link and read Blob slices, including `blob:` URLs. The Python MCP backend writes a new exclusive file, verifies actual bytes/SHA and returns a path receipt without inline Base64. There is no arbitrary JavaScript tool, new manifest permission, global download selection, desktop interaction or profile. Fetch errors remain errors; ephemeral button-only and POST downloads are not covered. Partial disk-write failures are reported without complete:true and the owned incomplete path is retained for inspection, never mistaken for a completed file.

These actions have prepared transport/controller/file tests; no simulated result establishes Brave or Faustus-agent acceptance. Existing no-replay, target/ref and disconnect semantics remain in force.

The fixed download readers verify that the anchor stays connected to the same current document, resolve its actual base URI, and compare href/download/base/document again after reading. Detached or changed anchors fail without publishing a transfer. Data URL contents are omitted from public source metadata (scheme and omission flag remain); the file bytes stay in the private transfer and local file. Prepared tests include Unicode/binary data URLs, retained detached anchors and relative href with a distinct/changing base on an owned loopback server; no real-browser result is claimed.


## Verified file-action status (2026-11-02)

File transfers and dispatched-request cancellation were independently reviewed and integrated locally as 0355148f. The integrated source passes 96 Python and 37 extension tests. Five additional independent stdio/WebSocket/AnyIO controls verify cancellation and replacement-peer cleanup; their extension peer is simulated. The original cancellation probe remains a recorded failure in its client cleanup after its strict connection-close assertion passed.

The managed backend was restarted normally and its live browser_session catalog now lists upload and download alongside the original eight actions. The existing disabled direct Writer and legacy QA MCP registrations remain disabled. No model service was stopped or started.

Real Brave file-chooser/Blob acceptance and new Faustus-model acceptance remain pending: the installed extension still needs a normal reload, and its loaded folder/code version is not established by its ID or handshake. Browser automation rejected the internal extension page; no desktop/CDP workaround was used. The earlier prepared/under-review wording above is historical, not the current review state. Writer parity remains open.


## Looking and moving like a person (1.2.0, verified 2026-11-06)

The extension now covers what a person (or Claude driving a tab) does to read a page, all on an explicit tab and in the background:

- `reload` (optional `ignore_cache`), `back`, `forward` and `navigate` wait for the new document (new loader, document stable for 600 ms) before the readback, and the readback retries up to three times while a page is still replacing itself.
- `scroll {direction up|down|left|right, amount}` turns the wheel at the centre or over a `ref`; a `ref` alone scrolls it into view. `hover {ref}` opens menus and tooltips. `select {ref, value}` picks an option by value or visible text.
- `wait {seconds ≤ 10}` gives a slow list or a check page time and reads it again; nothing is sent to the page.
- `screenshot` takes `region [x0,y0,x1,y1]` or a `ref` to zoom on part of the viewport.
- `open_tab {url}` opens a page in a new background tab; `close_tab` closes only tabs opened that way (`tabs` marks them `opened_by_faustus`). `reload_extension` makes the browser load new extension code.
- `view: {text: true}` (Python side) turns any readback into reading lines: text in document order, controls as `[role ref] label`, disabled marked, links with `-> url`, filtered by `query` and paged by `offset`/`limit`.
- `view: {visible: true}` keeps only what is on screen now. The extension measures it with one `DOMSnapshot.captureSnapshot` of layout boxes against the scrolled viewport (only when asked) and returns `viewport {x, y, width, height, page_width, page_height}`; the text view reports `off_screen_lines`.

Reconnection: after the relay goes away the extension retries after 1, 2, 5 and then every 10 seconds (the one-minute alarm stays as the fallback), and the relay lets a call wait up to 8 seconds (`--peer-wait`) for the extension to come back instead of failing at once. In the same Faustus turn, a relay restarted with a new contract is announced live from the next step, so its calls are no longer refused until the next turn.

Verified live in Brave through a Faustus agent turn on a real shop list: `reload` with text view (2 calls, 33 s instead of 31 calls), link URLs, `wait`, `scroll` down and up with `visible` (80 lines on screen and 326 off screen at y=720; 57 and 362 back at the top), background `open_tab`, region screenshot and `close_tab`. A check page in a background tab was reported and left alone. 49 extension tests and 102 Python relay tests pass.

## Human turns and local capture (1.3.0)

The extension panel provides **Te toca**, **Continuar**, direct tab adoption and
saved session names. The existing `browser_session` MCP tool exposes
`sessions`, `session_save/open/forget`, `handoff`, `handoff_status`, `adopt_tab`,
`har_start/stop`, `recording_start/stop` and `last_minute`.
The [full contract and verified limits](../src/browser_relay/README.md#human-turns-saved-sessions-and-capture-130)
include restart behavior, real-click semantics, opt-in HAR bodies, ffmpeg WebM
encoding, optional recording cursor, background capture and failure-minute
receipts. The fixed recording cursor is additional product-owned page code;
callers still cannot supply JavaScript. No manifest permissions were added.

The 2026-11-11 isolated real-browser/stdio-MCP test completes 27 calls, saves
HAR and playable WebM, distinguishes synthetic and browser-generated clicks,
and retains names after a full browser restart. 130 Python and 56 Node tests pass.
An extension-code reload in that headless browser unloaded the extension; automatic
recovery for that case is not established. This does not claim a new model-agent
acceptance run or changes to the installed user extension.
