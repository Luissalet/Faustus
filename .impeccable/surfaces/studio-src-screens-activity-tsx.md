---
version: 1
slug: "studio-src-screens-activity-tsx"
primary_target: "studio/src/screens/Activity.tsx"
related_targets: ["studio/src/adapters/activity.ts","studio/src/screens/activity.css","studio/src/lib/activity-poller.ts"]
---

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

FINISH: independent reviewer verdict **ship**, no required fixes. Documented in
`docs/ui/activity-live-work.md`; inherited root `DESIGN.md` and
`.impeccable/design.json` are unchanged. No identity workshop or new visual world.

## Implemented surface contract

- Live conversations use server-reported running, queued or awaiting-permission
  state. Detail explains the phase and available elapsed/last-progress information;
  quiet model output is not proof of a stall. Open conversation returns to
  `/studio?s=<sessionId>`; Stop targets the exact session/run pair.
- Failed sources retain their own last-known rows while successful sources
  replace theirs. Repeated failures do not duplicate rows, recovery clears
  staleness, and a confirmed empty conversation source removes ended work.
  Missing names fall back without making fresh run state stale.
- Partial/total connection warnings sit outside the list/detail layout and remain
  visible in mobile detail. Rows and detail label stale state; a failure is never
  presented as a successful empty list. Initial total failure offers Retry.
- Stale server-changing actions are disabled and guarded: Stop, approvals, task
  rerun/parallel run/cache clearing, creating a chat from a result, and render
  cancellation. Opening an existing conversation and read-only links remain usable.
- Heading Refresh is manual recovery with loading and last-checked feedback.
  A single polling owner schedules live/unavailable work at 5 seconds, idle or
  failed reads at 30 seconds, pauses scheduled reads while hidden and refreshes
  on visibility return. In-flight reads may finish while hidden. A 15-second
  abort deadline and revision guards reject timed-out, superseded and unmounted
  responses.
- At 899 px or less, detail replaces the list. Opening hands focus to its `h2`;
  All activity restores the originating row when present. Buttons, chips, tabs
  and detail links have at least 44 px control height. At 540 px or less, row
  summaries wrap beneath kind/status and search fills the width with 1 rem text.
- Kind chips use `aria-pressed`; status tabs retain `aria-selected`, and selected
  rows use `aria-current`. English/Spanish copy and localized time/number
  formatting use the existing language system. Existing reduced-motion behavior
  and Faustus tokens remain authoritative.

## Review provenance

The implementation pass verified mobile focus using the DOM and navigation to
`/studio?s=working`. The reviewer independently inspected the code, the five
captures and the passing `activity-feed.check.mjs` and `activity-poller.check.mjs`
checks; disposition: ship, no fixes required.

All captures below are synthetic scenarios rendered with the actual Activity
component and CSS, not real LLM, shell or microphone runs and not shipping UI
image assets:

- `.impeccable/review/activity-desktop.png`
- `.impeccable/review/activity-mobile.png`
- `.impeccable/review/activity-partial-mobile.png`
- `.impeccable/review/activity-offline-mobile.png`
- `.impeccable/review/activity-user-1920.png`

This bounded documentation pass performed no additional browser test, rebuild
or polish, and introduced no new raster, dependency or permission. Later
authorized improvements remain uncommitted; no staging, commit or push occurred.
