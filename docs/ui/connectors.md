# Connectors (Studio) — CONTRATO_CONECTORES Lote F3

`/connectors`. One unified list of every app the agent can reach through
MCP: the Hoard presets (Jobhunter, Writer's Hoard…) and every other MCP
server, exactly as `GET /api/app-connectors` (Lote F1) returns them. Mail and
Calendar are **not** duplicated here — the "Add" menu links to their real
forms in Settings → Integrations and to `/calendar` instead of building a
second one.

This lote is written against the F1/F2 contract (`docs/api/app-connectors.md`,
`docs/api/tool_selection.md` once those lots land the routes). Every call
goes through `studio/src/adapters/connectors.ts`; a route that does not
exist yet on a given build answers 404 the normal way and the screen shows
that as a message (`Could not read the connector list.`), never an
unhandled crash.

## Files

- `studio/src/adapters/connectors.ts` — presets, connectors, tools, launch
  and launch-profile CRUD. Types match F1.1/F1.3/F1.5 exactly.
- `studio/src/adapters/sessions.ts` — `getSessionConnectors`/
  `setSessionConnectors`/`getSessionToolSupport` (F2.2/F2.4).
- `studio/src/adapters/projects.ts` — `Project.connector_ids` +
  `setProjectConnectors` (saves independently of the rest of the project
  form, the same way the board key does).
- `studio/src/adapters/automations.ts` — `Automation.connector_ids` /
  `TaskInput.connector_ids`.
- `studio/src/screens/connectors/Connectors.tsx` — the screen.
- `studio/src/screens/connectors/NewConnectorForm.tsx` — alta desde preset
  (one field per `preset.placeholders`, pre-filled only from
  `preset.defaults` — never a path of Luis's own machine).
- `studio/src/screens/connectors/LaunchProfiles.tsx` — launch profile
  editor: executable, argv as an editable **list** (never a shell string),
  cwd, readiness URL/timeout. User-only; the agent has no tool that reaches
  this.
- `studio/src/screens/connectors/ToolsDrawer.tsx` — "View tools" drawer,
  reusing `setMcpDisabledTools` (the same toggle Settings → Integrations
  already has for any MCP server).
- `studio/src/screens/connectors/ConnectorPicker.tsx` — the reusable
  multi-select (see below).
- `studio/src/screens/connectors/connectors.css` — only what is genuinely
  new (row layout, the seven-state chip); everything else reuses
  `fs-screen`/`fs-panel`/`fs-set__*`/`fs-tools`/`fs-chip`/`fs-notice` from
  `projects.css` and `settings.css`.

## The seven states

Every row shows a chip for `status.state` — `unconfigured`, `app_off`,
`connecting`, `available`, `error`, `disabled`, `unknown` — icon **and**
text, colour only reinforcing (never the only signal). `App:` and
`Adapter:` are shown as two separate facts (`statusLine()` in the adapter),
because a `tools/list` answering is not proof the domain app itself is
running (F1 principle 3) — `unknown` is a real, visible answer, never
folded into "disconnected". Any `reasons` the backend attaches are behind
a "N reasons" disclosure, expanded to a plain list, never just a tooltip
that a keyboard or a screen reader user cannot reach.

Actions on a row: **Connect/Disconnect** (primary), **Check now**, and a
menu (**Start the app**, **Open the app**, **View tools**, **Edit** —
presets only —, **Remove**). Every action that cannot apply right now is
disabled with its own reason (`reasonForDisabledAction`), e.g. "Configure a
launch profile first." for Start when there is none. Connect/Start/Open
disable themselves while in flight, so a second click cannot start a
second process — idempotent in the UI on top of F1's own idempotent
routes.

## Launch profiles

User-authored only. The form validates the shape the same way the
contract asks the backend to (absolute executable, argv as a list, cwd for
a `process` profile) — real enforcement still lives in F1; this is the
form not letting you submit something obviously wrong. Nothing here is
reachable by an agent tool.

## ConnectorPicker

`studio/src/screens/connectors/ConnectorPicker.tsx` is the one reusable
component `connector_ids: string[] | null` gets edited with everywhere
(F2.2's own precedence: session/task explicit > project > "every enabled
connector"). `value: null` means "inherit" and shows what that resolves to
right now (`effective`/`source`, when the caller has them) instead of a
blind guess. Picking any connector while inheriting starts the explicit
list from that same `effective` set, so switching away from "inherit"
never silently drops what was already working.

Mounted in:

- **Session settings** — `studio/src/screens/studio/Composer.tsx`'s
  `SessionConnectorsSelector`, a chip next to the behaviour-mode chip
  (`BehaviorModeSelector`) and the model picker, opening a popover with the
  picker. Backed by `getSessionConnectors`/`setSessionConnectors`
  (F2.2). Hidden until a session exists — there is nothing to persist to
  before the first message creates one, the same way several other
  per-session features in that composer already behave.
- **Project** — `studio/src/screens/project/Settings.tsx`, right after the
  board-key field, saving on every change through `setProjectConnectors`
  independently of the project form's own "Save changes" (again, the board
  key's own pattern).
- **Task / automation form** — `studio/src/screens/automations/Form.tsx`,
  inside the "Model, chaining, connectors and notifications" `<details>`,
  included in the same `TaskInput` the rest of the form already builds.

When `GET /api/sessions/{id}/tool-support` (F2.4) reports
`supported: false`, the picker shows an inline notice — "This model does
not support tools; connectors will not be used." — and nothing switches
model or endpoint on its own; only the session chip wires this in today,
since it is the one mount point with a live model/endpoint to ask about.

## Routing

- `shell/routes.ts`: `/connectors` in `TOOLS` (icon `Plug`, reachable from
  the sidebar's tools group and the command palette for free) and in
  `SERVER_ROUTES`.
- `studio/src/shell/AppShell.tsx`: lazy route `<Route path="/connectors" element={<ConnectorsScreen />} />`.
- `app.py`: `@app.get("/connectors")` added next to the other declared SPA
  paths — the only line this lote touches in `app.py`.
- Deep link: `/connectors?id=<id>` opens the tools drawer for that
  connector (also how "View tools" and a freshly-created connector land
  you there).

## Diagnosing a state

| State | What it means | What to do |
| --- | --- | --- |
| `unconfigured` | A placeholder is empty, or a referenced file/folder does not exist | Edit the connector and fill in what is missing |
| `app_off` | The health check got connection-refused/timeout | Start the domain app (a launch profile makes "Start the app" work) |
| `connecting` | The MCP adapter is mid-handshake | Wait, or Check now |
| `available` | Health OK and the adapter reports tools | Nothing — View tools to enable/disable individual ones |
| `error` | Health responded but something is wrong (wrong service on that port, adapter error) | Read `reasons`; usually a wrong `APP_URL`/port |
| `disabled` | `is_enabled` is off | Re-enable it (edit or the Integrations toggle) |
| `unknown` | Not checked yet, or the backend genuinely cannot tell | Check now |

## Pending / degrades gracefully

This worktree does not carry F1's backend or F2's session/project/task
routes — they are separate lots, integrated later. Every adapter call in
this lote is written to the documented contract and fails as an `ApiError`
with the server's own message on a 404, which every screen here shows
rather than crashing on. Once F1/F2 land in the integrated tree, no Studio
code changes should be needed — only the previously-404 routes start
answering for real.
