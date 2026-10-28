# Harness refinement proposals

After a task, a local model may propose the smallest edit to the harness state
around the immutable base prompt. By default a proposal is first **probed**:
the task is repeated read-only with the harness as it is and with each
candidate edit, and only the edit that earns it is applied, by itself. With
the probe off, or auto-apply off, a person approves. Every applied edit can be
undone by id.

Code: `src/harness_refinement/` (store, targets, signals, proposer, runner,
and for the probe: `probe`, `rollout`, `compose`, `validator`, `experience`,
`probe_store`), `routes/harness_proposals_routes.py`, MCP tools
`harness_proposals`, `harness_probes`, `harness_lessons` in the `harness`
server. Studio: Skills > Harness proposals.

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
proposes with the harness endpoint at temperature 0 and a JSON response
schema. It never loads a model on its own and never runs in the foreground
(the same admission checks gate every call of a probe). A session with a
pending proposal gets no second one, and the same evidence is not proposed
twice.

## Probed refinement

Settings: `harness_refinement_probe` (default on), `harness_refinement_auto_apply`
(default on, only matters with the probe on), `harness_refinement_probe_k`
(repeats of the unmodified harness, default 2). With the probe off the job
behaves as before: one proposal, waiting for a person.

One round (`probe.probe_for_session`):

1. **Propose** on two non-overlapping lines, at most one candidate each:
   `inv` (how the agent works: process, tool use, checks) and `dom` (what it
   knows about the domain: facts, names, conventions). The proposer first reads
   the project's `EXPERIENCE.md` as hypotheses to check, not as facts. A
   candidate that does not pass `validator.check_edit` is dropped.
2. **Probe**: the original task (never the person's later correction) is
   repeated in the background against the global model through the agent loop
   with the `read_only` preset and `unattended`: the unmodified harness `k`
   times, each candidate once. Variants are overlays on the system context;
   no real store is touched. Each run is recorded with its steps.
3. **Compose**: an aggregator sees the original run, the base repeats and the
   candidate runs, and the correction as "what the person made explicit
   later". It accepts an edit only citing a moment of a run that exists
   (`{run, step, quote}`); a failure in only 1 of `k` observed runs is noise
   (`failure_not_repeated`); `keep_base` is a valid answer. Enforced in code
   (`compose.enforce`), not only in the prompt.
4. **Select blind**: the deliverables of the base repeats and the accepted
   candidates go in with shuffled ids and a recorded seed; the base can win.
5. **Validate** the winner once more (`validator`: skills named in the edit
   exist, cited scripts exist, no broken `{{ }}` / `{% %}` templates); on
   failure the base stands.
6. **Adopt**: the winner is stored with its provenance (`probe_id`,
   `probe_line`, `probe_result`, `probe_reason`, `probe_evidence`,
   `probe_verdict`) and applied with `by="harness-probe"`, or left pending when
   auto-apply is off. Every non-winner is stored as rejected with the reason
   and its evidence. The exact undo (`POST .../{id}/undo`) works as for any
   applied edit.
7. **Lesson**: one entry may be appended to the domain's `EXPERIENCE.md`.

### EXPERIENCE.md

Per domain (a project): `<workspace>/.faustus/EXPERIENCE.md`, listed in the
project's `MEMORY.md`; domains without a workspace fall back to
`DATA_DIR/harness_proposals/experience/`. Entries are `cue`, `lesson`,
`evidence`, `confidence` and a verdict: `helped`, `hurt`, `no observed effect`
or `confounded`. `NO_ENTRY` is the right answer when the round shows no
contrast; an entry with only a confounded verdict, an incomplete one, a near
duplicate (similarity 0.85 or more) or one that trips the memory security scan
is not written. At most 30 entries are kept. `helped` needs the aggregator's
acceptance and the blind win; otherwise it is capped at `confounded`.

### Probe endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/api/harness/proposals/probes?status=&session_id=&limit=` | light rows, newest first |
| GET | `/api/harness/proposals/probes/{id}` | full record (task, rollouts with steps, compose verdicts, selection, experience) and the proposals it created |
| POST | `/api/harness/proposals/probes` `{session_id, force}` | admin; starts a round in the background and answers at once with `{probe_id, status: "running"}` |
| GET | `/api/harness/proposals/lessons?project_id=` | without `project_id`, the domains with a lessons file; with it, its entries |

A round that is running when the process stops is marked `aborted` the next
time the list is read. Records are capped at 120.

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
