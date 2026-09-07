# Activity: live conversations and connection recovery

Scope: extend `studio/src/screens/Activity.tsx`; Operate mode. Existing
Activity list/detail composition, routes, user theme and design tokens remain
authoritative. No new visual identity, dependency, or agent permission.

## Direction contract

THESIS: Activity must include ongoing conversations; an empty list must never
stand in for a failed connection.

OWN-WORLD: inherit Faustus surfaces, semantic state colors, compact rail rows
and existing buttons. No new palette or decorative metrics.

STORY: find work needing attention, read its real phase, return to its chat.
Stop names the exact conversation run; conversation permission requests remain
inside their conversation. Existing standalone approvals retain their detail
actions.

FIRST VIEWPORT: existing heading gains refresh and connection feedback;
conversations join the filtered list. The detail pane explains background
execution, elapsed time and last progress, with Open conversation first.

FORM: local extension of an established Operate surface; no seed required.

FINISH: reviewed with a ship verdict and no required fixes. This document and
the matching surface brief capture the implementation; root `DESIGN.md` and
`.impeccable/design.json` remain inherited authority and are unchanged.

## Implemented behavior

- Conversations come from server-reported running, queued and awaiting-permission
  activity, never from conversation age. Waiting takes precedence over running;
  a missing conversation name falls back to “Conversation” without hiding the run.
- Conversations share the existing status filters, search and kind chips with
  tasks, renders and approvals. Kind chips expose selection with `aria-pressed`;
  status tabs retain `aria-selected`, and the selected row uses `aria-current`.
- Conversation detail shows the reported phase, model and available elapsed,
  last-progress and round information. Last progress means model/tool output,
  not a connection heartbeat; quiet output is not presented as proof of a stall.
- “Open conversation” navigates to `/studio?s=<sessionId>`. “Stop this run” uses
  that conversation's session and run IDs. Waiting conversations direct the user
  back to the chat to answer its permission request.

## Connection and refresh contract

Each source is refreshed independently. A failed source retains only its prior
rows, marked as last known activity; successful sources replace their previous
rows, including removing ended conversations when a fresh source is empty.
Repeated outages do not duplicate retained rows. A conversation-name lookup
failure warns about names but does not make successfully read run state stale.

When all sources fail, the last snapshot remains visible. Without any previous
snapshot, Activity shows a read failure and Retry, not a successful empty state.
Partial or total failure with no matching rows explains that work may be
unavailable. The connection warning sits outside the list/detail layout, so it
remains visible while mobile detail hides the list. Retained rows and their
detail also identify last-known status.

Stale detail disables server-changing actions: conversation Stop, standalone
Approve/Deny, task Stop/Run again/Run another/Clear cache, creation of a chat from
a task result, and render cancellation. The action handler also rejects stale
or concurrent mutations. Opening an existing conversation and read-only links
remain available.

Manual Refresh stays in the heading, with loading feedback and the last checked
time. There is one polling owner: it schedules the next read after 5 seconds
when a source is unavailable or work is running, queued or waiting; otherwise
after 30 seconds. A failed read also uses the 30-second retry cadence. Hiding the
page pauses scheduled polling; returning to a visible page refreshes immediately.
An already-started read may finish while hidden. Each read has a 15-second abort
deadline. Superseding refreshes abort prior reads, and revision/abort guards
prevent late, timed-out or unmounted responses from updating the screen.

## Layout, accessibility and language

The existing compact rail, semantic status colors, Faustus tokens and shared
controls are preserved. Desktop keeps the list beside a sticky detail pane.
At widths of 899 px or less, detail replaces the list; opening a row moves focus
to the detail `h2`, and “All activity” restores focus to the originating row
when it remains present. Buttons, kind chips, status tabs and detail links have
a minimum mobile control height of 44 px. At 540 px or less, kind and status
sit above the row summary, search fills the width, and its text uses 1 rem.

Interface copy supports English and Spanish through the existing translation
system; times and numbers use the selected locale. Existing reduced-motion
handling remains in place. These are surface-specific behaviors, not changes
to the global responsive or visual identity contract.

## Finish evidence and limits

The implementation pass verified mobile keyboard focus through the DOM and
conversation navigation to `/studio?s=working`. An independent reviewer inspected
the code, captures and both checks below, and returned **ship**, with no fixes
required:

- `studio/checks/activity-feed.check.mjs`: live conversations, precedence, scoped
  run IDs, partial-source retention/recovery, cancellation and offline handling.
- `studio/checks/activity-poller.check.mjs`: supersession, late responses,
  visibility, unmount/restart, timeout and a single polling chain.

The retained review captures are all synthetic scenarios rendered with the actual
Activity component and CSS:

- `.impeccable/review/activity-desktop.png`
- `.impeccable/review/activity-mobile.png`
- `.impeccable/review/activity-partial-mobile.png`
- `.impeccable/review/activity-offline-mobile.png`
- `.impeccable/review/activity-user-1920.png`

These are review evidence, not generated UI assets or evidence of real LLM,
shell or microphone execution. The documentation finish pass added no browser
testing, rebuild or polish. No new raster asset, dependency or permission was
introduced. The authorized later improvements remain uncommitted; this pass did
not stage, commit or push them.
