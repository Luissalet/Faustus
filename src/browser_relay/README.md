# Browser session

Task 1671, Writers Agora thread 1808. Architecture: resolved thread 1811.

This backend connects Faustus to existing tabs through its own Chromium
extension. It uses the ordinary MCP manager and the existing administrator
boundary. The browser profile is shared with its human operator. It never
launches a browser, changes its startup flags, creates a profile, or sends
desktop input. The existing Playwright/CDP backend remains available.

The current acceptance target is the existing native Python installation.
Both Windows packaging manifests include the extension and backend sources,
but the portable executable's Python-worker launch has not been validated.
Including files alone does not establish portable runtime support.

## Implementation and acceptance plan

- [x] Inspect the existing browser backend and ask Faustus for a real-session test.
- [x] Agree on the product transport and error semantics in Writers Agora.
- [x] Implement the Python MCP/WebSocket transport and extension.
- [x] Exercise actual stdio/WebSocket connections with a clearly identified fake peer.
- [x] Exercise extension actions, stale references, detach and readback failures with simulated CDP.
- [ ] Review the current compact-presentation delta independently.
- [x] Install the extension through the normal browser flow.
- [x] Root's MCP client reads the real QA tab, opens Library and returns Home.
- [ ] Use Faustus to read Writer's existing QA tab, open Library and return Home.
- [x] Root's target stays in the background and the active tab is preserved.
- [ ] Demonstrate the task and timings through Faustus's model; global focus was not measured.
- [ ] Update product documentation, integrate with the merge lease, and leave no push.

Simulated transport/CDP results do not establish real Brave compatibility.
Root's separate real acceptance is described below. Task 1671 stays open for
normal Faustus-agent acceptance. Writer parity task 1095 remains open independently.

## Wire contract

The stdio server is `src/browser_relay/server.py`, builtin ID `browser_session`,
tool `browser_session`. Its WebSocket listens only on `127.0.0.1:7016` and
accepts only `chrome-extension://cneonpjikbdhbaddfpmmdjemfdphlppa`.
The manifest's fixed public key defines that ID; it is not a credential.
An occupied port is an error, never a reason to stop its owner or silently
choose another browser. A second extension peer cannot replace a live one.

The extension sends `{type:"hello", protocol:1, extensionId:chrome.runtime.id}`.
The server replies `{type:"ready", connection_id:"..."}`. A transport heartbeat
uses `{type:"ping"}` / `{type:"pong"}` and never repeats a command.
Commands are `{type:"command", id:"...", arguments:{...}}`; replies are
`{type:"result", id:"...", ok:true, result:{...}, phase:"readback_complete"}`
or `ok:false` with `error:{code,message}` and a known phase.

Actions: `tabs`, `snapshot`, `click`, `type`, `press`, `navigate`, `screenshot`,
`release`. Every action except `tabs` takes an explicit integer `tab_id`.
`click` and `type` require a reference from the latest snapshot of that tab.
`type` inserts Unicode text using browser input; it does not implicitly clear
the field. `press` can optionally focus a referenced node before a key/chord.
Navigation accepts HTTP(S). `release` detaches only this extension's debugger
and invalidates references; it never closes the tab or browser.

Snapshots contain tab metadata, a generation, main-frame document identity and
accessible nodes with references. References expire on a new snapshot,
navigation, detach or connection change. Cross-origin frames are outside the
first implementation's advertised scope. Mutations include a fresh snapshot
in the same result. Screenshots use the explicit target's `Page.captureScreenshot`
and become MCP image content, without duplicating image data in text.

`detail` is optional: `compact` by default, or `full`. This is a Python
presentation option and is removed before sending the command to the extension.
It applies both to a direct snapshot and to a mutation's nested snapshot.
Compact preserves original references, useful names, values, properties and
order. Empty wrappers are collapsed and their children promoted in order.
An InlineTextBox is removed only when its text is demonstrably represented by
an ancestor and it adds no properties. Nodes with `ref:null` have no resolvable
ancestry in this DTO: their content is retained, even if another node has the
same text. Empty/default node fields may be omitted. There are no node, text or
depth truncation limits. `full` preserves all original snapshot fields; an
unfamiliar graph that cannot safely be projected also retains its full form.

`view` optionally selects a flat page from the original AX nodes of `snapshot`
or the readback of `click`, `type`, `press`, and `navigate`. Like `detail`, it is
removed in Python before dispatch to the extension. For example,
`view:{query:"Biblioteca",roles:["button"],offset:0,limit:10}` selects a literal
case-insensitive substring of name/value AND an exact role. Query uses Unicode
casefold without accent or whitespace normalization. An omitted roles filter
accepts all roles; an empty array selects none. Offset defaults to 0; an explicit
limit must be an integer at least 1. Without limit, all matches from offset are
returned. There is no default truncation.

The selected snapshot has `selection:{kind:"flat",total,matches,offset,next_offset}`:
total is the original AX node count, matches is the count before pagination,
and next_offset is null at the end. Rows keep original order, references
(including null), names, values and properties; only child_refs is removed to
avoid presenting a partial selection as a complete graph. This view is selected
before compacting, so full/compact both return the same selected rows. Snapshot
tab/document/generation metadata and mutation action/phase remain unchanged.
Without view, compact/full retain the existing behavior. An unfamiliar snapshot
without a list of object nodes is returned unchanged, without claiming a flat
selection. Filtering reduces returned content, not AX acquisition time or the
cost of a model turn. This delta still requires independent review and separate
real MCP/Brave and Faustus-agent acceptance.

`view.context:true` adds `context_nodes` containing original direct parents and
all their addressable child rows (including the selected row). `nodes` stays
flat and pagination/counts stay unchanged. Context is for the selected page,
not every match; it preserves original raw order, duplicates, fields and
child_refs. It is partial context, not a closed subtree: no recursive expansion,
text synthesis, missing-link repair or ancestry inferred for selected null refs.
For split labels use `view:{query:"visibles",roles:["StaticText"],context:true}`.

Roles are case-sensitive: use `StaticText` or `button`, not `statictext` or
`Button`. If roles eliminates all pre-pagination query matches, selection adds
`matched_roles:{role:count}` for those original matches. It never substitutes a
role or changes matches=0. An out-of-range page alone does not emit this hint.

Failures distinguish `not_dispatched`, `outcome_unknown`, and
`action_confirmed` with incomplete readback. A CDP acknowledgement establishes
that a command ran, not that the application achieved the intended result.
Disconnects/timeouts after dispatch never cause automatic mutation replay.
The MCP tool is not annotated as read-only or idempotent, so the manager's
existing reconnect behavior cannot infer replay permission.

## Status

Root exercised 22 real MCP RPCs (20 tool calls) through the installed extension
in Brave, covering all eight actions, Library's 36 visible entries, screenshot
and release. The target remained inactive and the same five project entries
were visible before and after. A navigate acknowledgement with failed readback
was reconciled by reading again, without repeating the navigation. This was
Root's MCP client, not a completed Faustus-model turn or proof of portable
runtime support. Integration and normal-agent acceptance remain pending.

Seven focused presentation tests pass after retained causal failures. Offline
projection of immutable real recordings 006/013 reduces one JSON payload from
64,081 to 32,455 bytes (Home) and 217,658 to 144,642 bytes (Library). The unique
MCP text forms are 68,786 to 34,262 and 233,752 to 152,333 bytes; these counts do
not add the equivalent structured representation. All 67/280 null-ref nodes,
useful controls, text, values and original references are preserved. Full
matches the original recordings. This is an offline size result, not a new
Brave run, model-token measurement or reduction in AX acquisition time.


## Local file transfers (under review; no real-browser acceptance yet)

The same `browser_session` schema adds `upload` (`tab_id`, current `ref`, absolute `files`) and `download` (`tab_id`, current anchor `ref`, new absolute `output_path`). The eight original actions remain unchanged. Upload intercepts the target chooser and uses DOM.setFileInputFiles, then returns AX readback plus local input hashes; selection is distinct from application acceptance. Download alone uses fixed page-JavaScript readers of the observed link/Blob; no caller script or private app callback is accepted. It preserves binary slices and keeps transfer parts out of MCP text/structuredContent. The backend publishes only an exclusive real file receipt with bytes/SHA/complete and verifies the disk bytes; existing files are preserved. An incomplete write is an error and any partial owned file remains for inspection.

No new extension ID, key, permissions, port, download-history dependency or server is introduced. Blob revocation/document change/read errors fail without replay. HTTP fetch keeps its actual origin/CORS/CSP behavior. Ephemeral links created only inside a button callback and POST downloads are outside this first link-read contract. Activation and real MCP/Brave/model acceptance are separate and pending; no old blocked extension migration is retried by this code.

The fixed download readers verify that the anchor stays connected to the same current document, resolve its actual base URI, and compare href/download/base/document again after reading. Detached or changed anchors fail without publishing a transfer. Data URL contents are omitted from public source metadata (scheme and omission flag remain); the file bytes stay in the private transfer and local file. Prepared tests include Unicode/binary data URLs, retained detached anchors and relative href with a distinct/changing base on an owned loopback server; no real-browser result is claimed.

Cancellation before dispatch sends no command. Cancelling an already-dispatched MCP request retires its original extension connection, so existing disconnect cleanup aborts pending input/reads and releases that queue. Cleanup is shielded from the MCP AnyIO cancel scope and awaited by the cancelling owner; no background cleanup or command replay is scheduled. A replacement peer is not retired by the old request. Cancellation is propagated to MCP rather than announced as a completed action.


## Verified file-action status (2026-11-02)

File transfers and dispatched-request cancellation were independently reviewed and integrated locally as 0355148f. The integrated source passes 96 Python and 37 extension tests. Five additional independent stdio/WebSocket/AnyIO controls verify cancellation and replacement-peer cleanup; their extension peer is simulated. The original cancellation probe remains a recorded failure in its client cleanup after its strict connection-close assertion passed.

The managed backend was restarted normally and its live browser_session catalog now lists upload and download alongside the original eight actions. The existing disabled direct Writer and legacy QA MCP registrations remain disabled. No model service was stopped or started.

Real Brave file-chooser/Blob acceptance and new Faustus-model acceptance remain pending: the installed extension still needs a normal reload, and its loaded folder/code version is not established by its ID or handshake. Browser automation rejected the internal extension page; no desktop/CDP workaround was used. The earlier prepared/under-review wording above is historical, not the current review state. Writer parity remains open.

## Human turns, saved sessions and capture (1.3.0)

`browser_session` retains the previous actions and adds the following operations.
The extension keeps the same ID, endpoint and manifest permissions.

| Action | Arguments | Result |
|---|---|---|
| `sessions` | none | Saved names, last observed URLs, titles and tab IDs |
| `session_save` | `tab_id`, `name` | Persist a name in extension IndexedDB |
| `session_open` | `name` | Reuse the saved tab or restore the last URL in a background tab |
| `session_forget` | `name` | Forget the name; leave the tab and profile intact |
| `handoff` | `tab_id`, optional `reason`, `name` | Show the person a **Te toca** card |
| `adopt_tab` | `tab_id`, optional `reason`, `name` | Ask the person to cede a tab from the extension panel |
| `handoff_status` | `tab_id` | Observe waiting/continued and the current card ID |
| `har_start` | `tab_id`, optional `include_bodies` | Begin explicit CDP network capture |
| `har_stop` | `tab_id`, new absolute `output_path` | Write HAR 1.2 and return bytes/SHA-256 |
| `recording_start` | `tab_id`, new absolute `output_path`, optional `cursor` | Begin silent tab recording and last-minute buffering |
| `recording_stop` | `tab_id`, optional new absolute `output_path` | Encode WebM to the chosen start/stop path |
| `last_minute` | `tab_id`, new absolute `output_path` | Export the latest minute, or the minute frozen by an action error |

For a login, 2FA or payment step, call `handoff` with a concrete reason, let the
person finish it in the tab, and observe `handoff_status`. The extension panel's
**Continuar** / **Ceder pestaña** handlers require a trusted click with active user
activation; `.click()` from page JavaScript does nothing. No tool argument or
runtime message from a website can complete a card. Browser tools cannot attach
to the extension's own controls. During an explicit human turn, observations
remain available and tool input/closing that tab waits. Continue invalidates
references: take a fresh snapshot before acting. A stale card cannot complete a
newer request. The panel also lets the person cede their active web tab directly,
optionally naming it; `tabs` reports human-turn/adoption state.

Names, URLs and titles survive worker and browser restarts, follow observed
navigation, and are the only journey metadata stored locally. Closing a tab does
not erase its name. Sessions share the existing browser profile and its normal
login persistence; they do not clone cookies or isolate accounts. If the original
tab no longer exists, explicit `session_open` restores its last saved URL and
returns `restored:true`. Read it after loading. Capture does not restart itself.

HAR includes observed requests, redirects, responses, query fields, timings and
network failures. Response bodies are opt-in; unavailable bodies carry a comment.
Only traffic observed after `har_start` is captured. Browser/CDP timing and body
availability remain authoritative; this does not claim packet-level capture.

Video uses timestamped JPEG compositor frames (up to five per second), with
targeted screenshot fallback for inactive tabs. It never activates the tab.
`cursor:true` installs a fixed visible mouse marker for this recording and removes
it and its new-document hook on stop/release/disconnect. There is no caller-script
execution API. WebM encoding uses the family's local ffmpeg discovery. The full
recording stays in extension memory until stop; keep long sessions in manageable
recordings. The relay separately retains a rolling minute so an extension/browser
disconnect does not lose the recent evidence. Action errors save
`<recording-stem>.failure-<unique-id>.webm` beside the chosen output and return a
`failure_recording` receipt while preserving the original error and phase. Encoder
errors are separate `artifact_error` fields. `last_minute` can export the relay
buffer while the extension is unavailable. Release/closing a tab stops capture;
export the full recording before release. No audio or capture before recording
starts is available. A restarted relay has no in-memory frames from its predecessor.

All exports use exclusive creation, preserve existing paths and return disk byte
counts/SHA-256. HAR bytes and JPEG/video payloads never appear inline in MCP.
No capture is enabled by connecting, listing tabs or saving a session.

### Acceptance and limits

`tests/browser_relay/live_journeys.py` is an opt-in test using the real extension,
ordinary stdio MCP and an owned loopback fixture in an empty temporary browser
profile. It maps only that browser's fixed relay URL to an ephemeral test port;
production source, installed extensions and running apps are untouched. The
2026-11-11 run completes 27 real MCP calls covering trusted vs synthetic Continue,
adoption, HAR redirects/bodies, WebM, error freezing, export after disconnect and
names retained after a full browser-process restart. Dark/light and enlarged-text
panel captures were inspected. The area suite passes 130 Python and 56 Node tests;
39 additional browser-manager integration tests pass with the project's Python environment.
The system Python reports two warnings for an unavailable pytest-timeout plugin;
the project environment completes the area suite without them.
These are MCP-client/browser results, not a new autonomous model evaluation.

A separate attempt to exercise `reload_extension` in the headless test browser
unloaded its extension and subsequent extension-page navigation was blocked.
That attempt is retained as a failure; it does not establish automatic recovery
after an extension-code reload. No browser permission or installed user extension
was changed to work around it. Full browser restart with normal loading passes.
Portable-worker packaging and audio capture are outside this verification.

Example (absolute output paths must point to an existing local directory):

```json
{"action":"recording_start","tab_id":42,"output_path":"C:/work/session.webm","cursor":true}
{"action":"har_start","tab_id":42,"include_bodies":true}
{"action":"handoff","tab_id":42,"reason":"Completa el acceso y pulsa Continuar en Faustus Browser."}
{"action":"handoff_status","tab_id":42}
{"action":"har_stop","tab_id":42,"output_path":"C:/work/network.har"}
{"action":"recording_stop","tab_id":42}
```
