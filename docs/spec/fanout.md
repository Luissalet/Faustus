# Fan-out: one prompt, N candidate models, scored automatically (R3, Reach wave)

`src/fanout/` — orca's own pitch ("fan one prompt across five agents, each
in its own isolated git worktree — compare the results and merge the
winner", INFORME §2) plus OpenMontage's multi-dimension provider scorer
(INFORME §3): the same fan-out, but the comparison is a weighted score
with a logged reasoning instead of a person eyeballing N diffs, and the N
candidates need not be the same model — a free local Qwen worker races a
paid remote one on equal footing.

## What already existed and was reused, not duplicated

* **`src/alternatives.py`** — isolation (`worktree` for a git repo,
  `snapshot_dir` for a plain directory), `run_tests`, the three-way
  `git merge-file` apply/`compare`. Every fan-out candidate is one
  `alternatives` alternative under one shared experiment; `fanout_apply`
  is a thin wrapper over `apply_alternative`.
* **`src/agent_tools/subagent_tools._run_subagent`** — actually runs one
  worker with tools inside a workspace. `runner.py` calls it once per
  candidate, workspace pinned to that candidate's isolated alternative
  path, so a worker literally cannot touch another candidate's files.
* **`src/budget_account.py`** — reserve before launch, reconcile after,
  the same run-wide ledger `delegate_agents` already uses.
* **`src/project_tests.py`** — `detect_test_command` finds the target
  project's own test runner; `runner.py` feeds that command to
  `alternatives.run_tests` inside each candidate's copy.

`src/council/` (multi-model dialogue with an injected `ModelInvoker`) was
read but not reused directly: it orchestrates a conversation between
model participants, not N independent coding workers each editing files —
a different shape than what a fan-out race needs.

## Layout

```
src/fanout/
  plan.py     FanoutPlan / FanoutCandidate, from_settings_default_candidates()
  runner.py   start() + run_all(): isolation, workers, checkpoints, budget, concurrency
  score.py    OpenMontage-style weighted ranking with per-candidate reasoning
  merge.py    apply_winner() (two-step propose/confirm), diff_between()
  service.py  start/status/results/apply front door + background scheduling

src/agent_tools/fanout_tools.py   fanout_run / fanout_status / fanout_results / fanout_apply
routes/fanout_routes.py           POST /api/fanout, GET .../{run_id}, .../results, .../diff, POST .../apply
```

### Storage

`DATA_DIR/fanout/<run_id>/`:

* `run.json` — the plan, owner, `alternatives` experiment id, overall
  status (`queued`/`running`/`done`), timestamps.
* `<slug(label)>.json` — one candidate's checkpoint: `state`
  (`queued`/`running`/`done`/`error`), `alt_id`, `diff` (stats vs. base),
  `tests` (the project test run's result), `cost`, `latency_s`, `error`,
  `worker_report` (the worker's own stop reason/tool calls/mutations).

### Resumability

`runner.run_all(run_id, owner)` only ever touches a candidate whose
checkpoint is NOT `done`/`error`/`scored`. Calling it a second time — after
this process died mid-run and left a candidate's checkpoint at `running`,
or simply because a first call was interrupted — picks up exactly the
unfinished candidates. There is no separate `resume()` state machine to
drift out of sync with `run_all`; `service.resume()` is a plain alias for
`service.schedule()`, which calls `run_all` again.

### Concurrency

`agent_fanout_max_parallel` (default 2, setting) bounds how many
candidates run their worker at once overall. Additionally, candidates
whose endpoint looks local (`localhost`/`127.0.0.1`/empty) are serialised
to exactly one at a time regardless of that ceiling — two big local models
rarely fit one card's VRAM (the reasoning `DelegateAgentsTool`'s own
worker-slot semaphore already documents; a full `src/vram_fit.py`
admission check across arbitrary local pairs is left for a follow-up, this
is the cheap heuristic version).

### Budget

`plan.budget_tokens` (0 = no ceiling) opens a `budget_account` run keyed
by the fan-out's own `run_id`. Each candidate reserves
`max_rounds * DEFAULT_RESERVE_TOKENS_PER_ROUND` tokens BEFORE its worker is
launched; a reservation that does not fit is refused and the candidate
reports `state: "error"` with the budget reason, never silently launched.
Reconciled with the worker's actual token usage once it finishes.

### Scoring (`score.py`)

Six weighted dimensions, each normalised to `[0, 1]`, default weights:

| dimension   | weight | source |
|---|---|---|
| `tests`     | 0.35 | the project's own test run inside the candidate's copy |
| `harness`   | 0.20 | the worker's own stop reason/error/rejections (never its prose) |
| `diff_size` | 0.10 | additions+deletions vs. base; penalises a huge diff, but zero diff scores 0 too |
| `cost`      | 0.15 | lower is better; **unknown cost is never scored as free** |
| `latency`   | 0.10 | lower is better, normalised against the slowest candidate in the run |
| `judge`     | 0.10 | optional caller-supplied reviewer callable; neutral 0.5 with none given |

`fanout_results`/`GET /api/fanout/{run_id}/results` returns the ranking,
each row's dimension breakdown, and a one-sentence `reasoning` naming the
weakest dimension — "why did candidate X lose" is always answerable from
the result alone.

### Apply (two steps, same shape the Studio UI already gives `alternatives`)

`fanout_apply`/`POST /api/fanout/{run_id}/apply` with `confirm`/
`user_confirmed` unset previews the merge (diff stats, nothing written);
with it set, calls `alternatives.apply_alternative` for real — the
three-way merge that never discards a manual edit made to the main copy
since the run started, and raises `ApplyConflictError` (nothing written)
on any conflicting file rather than guessing.

## Tools

`fanout_run {prompt, candidates?, max_rounds?}` → `{run_id, candidates}`.
`fanout_status {run_id}`, `fanout_results {run_id}` — read-only.
`fanout_apply {run_id, label, user_confirmed?}` — gated like `alt_apply`.

## Routes

`POST /api/fanout`, `GET /api/fanout/{run_id}`,
`GET /api/fanout/{run_id}/results`, `GET /api/fanout/{run_id}/diff`
(`?label_a=&label_b=`), `POST /api/fanout/{run_id}/apply` (`?label=`).

## What is left for a follow-up

* No SSE events were added: `runner.py` accepts an `on_update` callback
  (per-candidate checkpoint dict) but nothing in this lot wires it to
  `/api/chat_stream` or a new SSE channel — per the lot's own rule
  ("si no los emites, no los declares"), `docs/api/sse_events.json` is
  untouched.
* The `judge` scoring dimension takes any `Callable[[dict], float]` but no
  caller wires `src.expert_review`/`src.claim_verify.verify` into it yet
  — it stays neutral (0.5) until one does.
* Concurrency's "local" detection is a URL heuristic, not a real
  `src/vram_fit.py` admission check across the specific models involved.
* A fan-out started via `fanout_run`/`POST /api/fanout` schedules itself
  as an `asyncio.Task` on the current process's event loop
  (`service.schedule`); a process restart loses that task object (not the
  checkpoints) — the caller must call `fanout_status`/`run_all` again to
  resume, there is no automatic background re-scheduling on startup yet.
