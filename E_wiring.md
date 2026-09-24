# E_wiring.md — handoff lanes + night shift

Two features, both fully self-contained in this lot's own files. The only
integrator-owned touch needed is registering the two new routers in
`app.py`; `tests/test_e_wiring.py::test_app_registers_the_handoff_lanes_and_night_shift_routers`
is `xfail(strict=True)` until that lands.

Everything else this lot needed edited that is NOT on the integrator list
(`src/agent_tools/subagent_tools.py`, `src/agent_settings_schema.py`,
`src/watchers.py`, `tests/test_agent_settings_schema.py`) was edited
directly in this worktree — see "Files touched" below.

## 1. `app.py` — register the two routers

```diff
+from routes.handoff_lanes_routes import setup_handoff_lanes_routes
+from routes.night_shift_routes import setup_night_shift_routes
```
next to the other `routes.*_routes` imports (e.g. beside
`from routes.dispatch_routes import setup_dispatch_routes`), and
```diff
+app.include_router(setup_handoff_lanes_routes())
+app.include_router(setup_night_shift_routes())
```
next to `app.include_router(setup_dispatch_routes())`.

No other integrator file needs a change: `agent_loop.py` is untouched
(the delegation hook lives entirely inside `subagent_tools.py`, which is
not on the integrator list for this contract).

## Part 1 — Handoff lanes

New files: `src/handoff_lanes.py`, `routes/handoff_lanes_routes.py`,
`tests/test_handoff_lanes.py`, `docs/api/handoff_lanes.md`,
`studio/src/adapters/handoffLanes.ts`,
`studio/src/screens/agents/HandoffLanes.tsx`.

Edited (not integrator-owned):
- `src/agent_tools/subagent_tools.py` — `DelegateAgentsTool.execute` calls
  `handoff_lanes.apply(runs, from_agent, depth)` right after
  `_attach_permissions` and before `_attach_resolution`/any worker starts;
  `SubagentRun` gained `lane_id`/`lane_disabled_tools`, folded into the
  `disabled_tools=` argument passed to `stream_agent_loop`. `off` (the
  default) is a byte-for-byte no-op — verified in
  `tests/test_handoff_lanes.py::test_off_leaves_parse_delegation_args_and_permissions_unchanged`.
- `src/settings.py` — appended `agent_handoff_lanes` ([]),
  `agent_handoff_lanes_mode` ("off").
- `src/agent_settings_schema.py` — appended two fields to the "Sub-agents"
  group (`agent_handoff_lanes` as a `list`, `agent_handoff_lanes_mode` as a
  `select`); `tests/test_agent_settings_schema.py`'s exact-set assertion for
  that group was updated to include them (both keys, both green).
- `studio/src/screens/agents/Defs.tsx` — a "Handoff lanes" button next to
  "Lint", opening `HandoffLanesPanel`.
- `docs/ui/i18n/es.tsv` — appended rows for every new Studio string.

### Live verification recipe
1. `POST /api/handoff-lanes` `{"lanes": [{"from":"main","to":"coder","tools_deny":["git_push"]}], "mode":"enforce"}`
   as an admin.
2. `POST /api/handoff-lanes/test {"from":"main","to":"coder","tools":["bash","git_push"]}`
   -> `{"allowed": true, "disabled_tools": ["git_push"]}`.
3. `POST /api/handoff-lanes/test {"from":"main","to":"reviewer","tools":[]}`
   -> `{"allowed": false, "reason": "no handoff lane covers ..."}`.
4. In a chat, `delegate_agents` a task naming `"agent": "coder"` — with the
   lane above in force, the worker starts but cannot call `git_push`; a
   task naming an agent with no covering lane is refused before any worker
   starts, with the message from step 3 (plus which lanes would allow it).
5. `GET /api/handoff-lanes/graph` -> Mermaid source with one edge per lane;
   paste into a Mermaid live editor to see it drawn, or view it in Studio →
   Agents → Definitions → Handoff lanes.

Not runnable in this sandbox (no live server / browser): steps 1-5 above
are exercised end-to-end by `tests/test_handoff_lanes.py`'s route-smoke and
`subagent_tools` integration tests instead.

## Part 2 — Night shift

New files: `src/night_shift.py`, `src/agent_tools/night_shift_tools.py`,
`routes/night_shift_routes.py`, `tests/test_night_shift.py`,
`docs/api/night_shift.md`, `studio/src/adapters/nightShift.ts`,
`studio/src/screens/agents/NightShift.tsx`.

Edited (not integrator-owned):
- `src/agent_tools/__init__.py` — imported `NightShiftTool`, registered
  `TOOL_HANDLERS["night_shift"]`, added `"night_shift"` to `TOOL_TAGS`.
- `src/tool_schemas.py` — appended the `night_shift` function schema.
- `src/tool_index.py` / `src/tool_index_examples.py` — appended the
  description and EN/ES example phrases.
- `src/tool_security.py` — added `"night_shift"` to `NON_ADMIN_BLOCKED_TOOLS`
  (same class as `delegate_agents`: it dispatches real workers).
- `src/tool_capabilities.py` — registered `{"night_shift"}` under
  `ToolEffect.EXECUTE_CODE` (same class as `delegate_agents`/`bash`).
- `src/settings.py` — appended `night_shift_default_max_minutes` (120),
  `night_shift_default_max_tasks` (8). (Not `agent_*`-prefixed, so no
  `agent_settings_schema.py` entry is required — confirmed by
  `tests/test_agent_settings_schema.py`.)
- `src/watchers.py` — appended `action_night_shift_report` /
  `WATCH_ACTIONS["night_shift_report"]` / its `WATCH_ACTION_INFO` entry:
  reports the latest finished shift's Markdown report, once per shift, so
  a scheduled task using this action can be pinned to Home
  (`src/home_cards.py` reads cards off a `ScheduledTask`, which this gives
  it).
- `studio/src/screens/agents/Workers.tsx` — mounts `NightShiftSection`
  (form + list) below the existing dispatch board, reusing the same
  `workspace` field.
- `docs/ui/i18n/es.tsv` — appended rows for every new Studio string.

### Live verification recipe
1. `POST /api/night-shift {"tasks": ["...", "..."], "workspace": "/abs/path", "budget": {"max_minutes": 30, "max_tasks": 2}}`
   as an admin -> a queued shift.
2. `GET /api/night-shift/{id}` while it runs -> `state: "running"`,
   `results` filling in one task at a time.
3. `POST /api/night-shift/{id}/stop` -> the shift finishes its current task
   then stops (`state: "stopped"`).
4. `GET /api/night-shift/{id}/report` -> the Markdown report.
5. Schedule a task with action `night_shift_report` (daily, morning) and
   pin it to Home: the card shows the latest shift's report once it
   finishes.
6. In Studio → Agents → Workers, the "Night shift" section at the bottom
   queues/stops/reports the same way.

Not runnable in this sandbox (no live server, no Ollama/dispatch worker
available here): step 1-6 above are exercised end-to-end in
`tests/test_night_shift.py` against a fake `dispatch` module standing in
for real workers, plus route-smoke and watch-action tests.

## Studio toolchain

`npm ci` + `npx tsc --noEmit -p tsconfig.json` from the worktree root: zero
errors, including the new files (`studio/src/adapters/handoffLanes.ts`,
`studio/src/adapters/nightShift.ts`, `studio/src/screens/agents/HandoffLanes.tsx`,
`studio/src/screens/agents/NightShift.tsx`) and the edits to `Defs.tsx` /
`Workers.tsx`. No browser/MCP screenshot pass was possible in this sandbox
(no running Faustus server, no browser tool attached); the recipes above
are what to run against a live instance to see both panels.

## Tests

`python3 -m pytest -q tests/test_handoff_lanes.py tests/test_night_shift.py`
— 69 passed. Guard suite also green:
`tests/test_tool_registry.py`, `tests/test_tool_index_schema_parity.py`,
`tests/test_l91_tool_index_examples.py`, `tests/test_external_context_tool_gate.py`,
`tests/test_agent_settings_schema.py`, and the existing subagent test files
(`test_subagent_board_events.py`, `test_subagent_board_scroll.py`,
`test_subagent_permissions.py`, `test_subagent_steer_routes.py`,
`test_subagent_stop_and_locks.py`, `test_subagents_v2.py`,
`test_delegate_effort.py`, `test_watchers.py`) — 394 tests, all green.
