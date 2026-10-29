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
