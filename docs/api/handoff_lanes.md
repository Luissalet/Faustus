# Handoff lanes

Permissions-as-topology: an explicit, inspectable policy of which agent may
delegate to which agent, with which tools. Module: `src/handoff_lanes.py`.

## Settings

- `agent_handoff_lanes` -- a list of lane objects (see below). Empty by
  default.
- `agent_handoff_lanes_mode`:
  - `off` (default) -- behaviour is byte-for-byte unchanged; the feature
    reads nothing on the delegation path.
  - `shadow` -- every delegation is evaluated and the decision is appended
    to `<DATA_DIR>/handoff_lanes/log.jsonl`; nothing is ever blocked or
    narrowed.
  - `enforce` -- a delegation not covered by any lane is refused, with a
    message naming the lanes that would have allowed it. A covered
    delegation has the lane's `tools_deny` added to the worker's own
    disabled-tool set, and, when the lane states `tools_allow`, any
    requested tool not in that list is added to the disabled set too. A
    lane can only narrow what an agent definition already allows, never
    grant beyond it.

## Lane shape

```json
{
  "id": "coordinator-to-coder",
  "from": "main",
  "to": "coder",
  "tools_allow": ["read_file", "write_file", "edit_file", "bash"],
  "tools_deny": ["git_push"],
  "max_depth": 1,
  "note": "the top-level chat may hand off to the coder agent, one level deep"
}
```

- `from` / `to`: an agent slug (`src/agent_defs.py`), `"*"` (any agent) or
  `"main"` (the top-level chat, never a worker).
- `tools_allow` (optional): the only tools this lane's handoff may keep;
  anything else requested is denied.
- `tools_deny` (optional): tools this lane's handoff may never keep,
  regardless of `tools_allow`.
- `max_depth` (optional): the deepest delegation level (1 = a direct
  child) this lane still covers.
- `note` (optional): shown next to the lane in the Studio panel.

The first lane (in stored order) whose `from`/`to` match and whose
`max_depth` is not exceeded is the one that applies.

## API

- `GET /api/handoff-lanes` -- `{"lanes": [...], "mode": "..."}`.
- `PUT /api/handoff-lanes` -- body `{"lanes": [...], "mode"?: "..."}`;
  validated before saving, so a malformed lane saves nothing.
- `GET /api/handoff-lanes/graph` -- `{"nodes": [...], "edges": [...],
  "mode": "...", "mermaid": "flowchart LR..."}` -- every known agent
  (builtins + library + user) plus one edge per lane.
- `POST /api/handoff-lanes/test` -- body `{"from", "to", "tools"?,
  "depth"?}`; a dry-run `evaluate()`, returns `{"allowed", "lane_id",
  "disabled_tools", "reason"}`. Never saves or logs anything.

All four are admin-only: a lane governs every agent's delegation on this
machine, the same trust class as an agent definition itself.

## Wiring into delegation

`src/agent_tools/subagent_tools.py`'s `DelegateAgentsTool.execute` calls
`handoff_lanes.apply(runs, from_agent, depth)` right after each worker's
own permissions are derived (`_attach_permissions`) and before any worker
starts. `from_agent` is the delegating agent's own slug (`"main"` for the
top-level chat), read the same way the permission derivation already
reads it. `apply()` is a no-op when the mode is `off`.
