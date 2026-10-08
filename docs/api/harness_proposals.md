# Harness refinement proposals

After a task, a local model may propose the smallest edit to the harness state
around the immutable base prompt. It only proposes. Applying needs a person;
every applied edit can be undone by id.

Code: `src/harness_refinement/` (store, targets, signals, proposer, runner),
`routes/harness_proposals_routes.py`. Studio: Skills > Harness proposals.

## Axes and targets

| Axis | Target | What is edited |
|---|---|---|
| `prompt_layer` | `project:<id>` | the project's custom instructions |
| `skill` | `skill:<name>` | `SKILL.md` (update only; uses the Skills proposal store and approval) |
| `memory` | `memory:<id>` / `memory:new` | one memory entry (max 6 changed lines, 600 chars) |
| `subagent_spec` | `agent:<slug>` | a user-level `AGENT.md` (never widens authority) |

Anything else is refused. The base system prompt, `*.py`, the agent instruction files,
absolute paths and `..` are never targets
(`harness.base_prompt_immutable`, `harness.bad_target`).

## Record

`{id, axis, target, op: create|update|delete, before, after, diff, rationale,
evidence: [{session_id, message_id, turn, kind, quote}], trigger, created_at,
status: pending|applied|rejected|undone, applied_at, undo_of, risk_flags}`.

An undo is its own record (`undo_of` set, `status: applied`). A trigger to
result log is kept next to the records (`GET .../log`).

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/harness/proposals?status=&axis=&session_id=&limit=` | newest first; pending rows carry `stale` |
| GET | `/api/harness/proposals/status` | setting, queue, counts per status |
| GET | `/api/harness/proposals/log?limit=&proposal_id=` | trigger to result log |
| GET | `/api/harness/proposals/{id}` | full record with exact `before` / `after` |
| POST | `/api/harness/proposals/propose` `{session_id, force}` | admin; works with the setting off |
| POST | `/api/harness/proposals/{id}/approve` | person only; idempotent (`already_applied`) |
| POST | `/api/harness/proposals/{id}/reject` `{reason}` | person only |
| POST | `/api/harness/proposals/{id}/undo` | person only; restores the exact previous content |

Approve, reject and undo sit behind `require_human`: the agent's in-process
token gets 403, so the model cannot approve its own proposal.

Errors are `{"detail": {"error_class", "message"}}`:
`harness.not_found` 404, `harness.target_changed` 409 (approve, target differs
from `before`), `harness.target_changed_since_apply` 409 (undo),
`harness.not_applied`, `harness.undo_of_undo`, `harness.base_prompt_immutable`,
`harness.bad_target`, `harness.too_large`.

## Automatic run

Setting `harness_refinement_enabled` (default **off**). When on, the end of a
turn queues one background job per session (bounded queue, one worker thread).
The job settles, runs the deterministic pre-filter (corrections, repeated
requests, retries, failed tools, clarifications; trivial successes are skipped
without a model), then waits for `background_job_guard` / model lease and
proposes at most one edit with the utility endpoint at temperature 0 and a JSON
response schema. It never loads a model on its own and never runs in the
foreground. A session with a pending proposal gets no second one, and the same
evidence is not proposed twice.

## Authority and concurrency

- A sub-agent spec edit never widens authority. Permission rules are ordered
  and the last match wins, so only two edits count as narrowing: dropping
  `allow` rules, and appending `deny` rules at the end. Reordering, inserting,
  or dropping a `deny` is refused (`harness.widens_authority`).
- Approve and undo compare the target with what the proposal was made against
  immediately before writing. For project instructions the compare and the
  write happen under the project store's own lock; for memory entries and
  agent files the compare sits next to the write, with no store lock to share.
  A change made by a person in between is never overwritten
  (`harness.target_changed`, `harness.target_changed_since_apply`).

## Limits

- With authentication disabled (single-user mode) any local HTTP caller that
  is not the agent's own internal token can approve. The model is kept out by
  `require_human` refusing that token, not by a secret the model cannot obtain;
  run with authentication on if the model has a shell that can reach the port.
- Memory entries and agent files have no store lock that edits made elsewhere
  also take, so a change landing in the few microseconds between the final
  compare and the write is still possible there.

- The prompt layer is the project instructions; there is no per-user custom
  instruction store to edit.
- Skill undo restores the file exactly but is not recorded in the sleep pass
  version history.
- Queue and in-flight guards are per process.
