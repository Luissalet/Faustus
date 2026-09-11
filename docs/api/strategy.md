# Strategy and recipes (CMP-09/CMP-12)

`GET/PUT /api/strategy/profile`, `POST /api/strategy/preview`,
`GET /api/recipes`, `POST /api/recipes/from-run/{run_id}`
(`routes/strategy_routes.py`, `src/strategy_policy.py`, `src/recipes.py`).

Makes how hard the agent works on a turn an observable, editable value
instead of a hidden heuristic buried in prompt construction — and lets a
successful task become a reusable, structured procedure ("receta") instead
of the whole skill haystack being handed over again next time.

## Two independent axes

* **`method`** — WHAT KIND of work this is:
  `direct_edit | plan_then_execute | research | specialised_review |
  explore_alternatives`. Picked from the task text by small, legible
  patterns (`src/strategy_policy.py::_classify_method`), never a numeric
  "confidence" the model reports about itself. The ONLY other thing allowed
  to change it is an escalation from **observable** evidence a prior
  attempt already produced (`failures_observed` / `uncovered_requirements`
  in `choose_strategy`'s `context`) — see the module's own docstring.
* **`profile`** — HOW CAREFULLY to do that same kind of work:
  `fast | balanced | deep_review`. Only scales the same method's
  steps/budget; it never itself picks a different method.

There is deliberately **no "council"/multi-agent-review method** in this
enum at all — `src/chat_team.py`/`routes/council_routes.py` remain an
explicit, user-invoked feature elsewhere in the codebase; nothing in
`choose_strategy` can ever produce a value outside its five methods (see
`tests/test_cmp09_strategy.py::test_never_defaults_to_council`).

## `Strategy`

```json
{
  "method": "direct_edit",
  "steps": ["locate the exact text/code to change", "apply the edit", "verify (tests, or a read-back of the changed spot)"],
  "budget": {"tokens": 4000, "time_s": 60, "calls": 3},
  "models_hint": [],
  "permissions_needed": ["file_write"],
  "close_criteria": ["the edit landed at the located occurrence(s)", "no unrelated file changed"],
  "reasons": ["task text matched the 'direct_edit' pattern"]
}
```

`budget` is a **forecast with assumptions**, not a measurement (CMP-08's
own vocabulary) — small, legible integers that order consistently across
profiles, never a claim about any real model's actual usage.

## `GET/PUT /api/strategy/profile?session_id=`

Persisted per owner, optionally scoped to one `session_id` (a session
override falls back to the owner's own default, which falls back to
`balanced` — see `src/strategy_policy.py::get_active`). `PUT` accepts
`{"profile": "fast"|"balanced"|"deep_review", "recipe_id": "..."|""}` —
either field alone, `recipe_id: ""` clears the active recipe. An unknown
profile is `400 strategy.invalid_profile`.

## `POST /api/strategy/preview`

`{"task_text": "...", "profile": "fast", "recipe_id": "...", "compare_profile": "deep_review"}`
→ `{"strategy": {...}, "diff": {...}}` (the `diff` key only when
`compare_profile` is a valid profile — `src/strategy_policy.py::profile_diff`,
pure, no I/O: "what visibly changes" between two profiles for the same
method, so the compositor can show a profile switch's consequence before
it applies to the next turn).

## Recipes (CMP-12)

```json
{
  "id": "edit-passage-keep-tone", "title": "Edit this passage without changing the tone",
  "inputs": ["..."], "steps": ["..."], "tools": ["..."],
  "success_conditions": ["..."], "optional_resources": [], "license": null,
  "status": "published"
}
```

Four built-ins ship in `docs/recipes/*.json` (read-only): *review these
changes*, *turn these sources into a report*, *design a function and
generate its tests*, *edit this passage without changing the tone*.
`GET /api/recipes` returns the built-ins plus the calling owner's own
drafts.

An active recipe (`context={"recipe_id": ...}` on `choose_strategy`)
**replaces** the method's generic `steps` with its own declared procedure —
one `Strategy` shape either way, never a second parallel "what to do"
concept. `src/agent_loop.py::_strategy_block` is where this actually lands
in the system prompt (see below); `src/recipes.py::procedure_block` is the
short, steps-only text it injects.

### `POST /api/recipes/from-run/{run_id}`

Turns one **successful** run into a `status: "draft"` recipe, owner-scoped,
saved under `DATA_DIR/recipes/<owner>/<id>.json`. Steps are the distinct
tool names the run actually used, in first-seen order (`use \`read_file\``,
...) — reconstructed from what happened, never invented. The run's own
`label` becomes the recipe title.

* `404 recipes.run_not_found` — no run log for that id.
* `400 recipes.run_not_finished` — the run has no recorded `done` status
  yet.
* Secrets: the **whole** raw run log is passed through
  `core.log_safety.redact_secrets` before anything is parsed out of it, so
  a secret in a tool argument or an assistant message is masked before it
  can reach the draft.
* Never promoted automatically — a human reviews a draft and promotes it
  separately; that review step is out of this module's scope (`status` is
  the seam for it).

## Turn-time wiring (`src/agent_loop.py`)

Two things, both gated on `not suppress_local_context and owner` and both
computed from the SAME `strategy_policy.choose_strategy` call:

* **`_strategy_block(owner, session_id, task_text)`** — a short prompt
  block ("## Strategy for this turn (profile: ...)") appended right after
  the project-board block, telling the MODEL the method's steps (or the
  active recipe's procedure) and `close_criteria`. Cached 5s per
  `(owner, session_id)` — short on purpose, so switching the profile or
  recipe in the compositor is felt on the very next turn, not the board
  block's slower 20s TTL.
* **`strategy` SSE event** — emitted once per turn (round 1 only, inside
  `_stream_agent_loop_body`'s round loop — the same generator that already
  emits `steer`/`harness_check`/`paused`/`plan_update` events, so it
  reaches the client through the same passthrough `chat_routes.py` already
  gives those, no route change needed) telling the UI/orchestrator what was
  chosen and why: `{"type": "strategy", "data": {"profile", "recipe_id",
  ...Strategy}}`.

## Límites / puntos a cablear por el orquestador

* `studio/src/adapters/chat.ts` does not yet have a `case 'strategy':` in
  its SSE switch (`chat.ts` is out of this lot's file scope — W2-D owns the
  next addition to it) — the event above is emitted and reaches the
  browser today, but nothing decodes it into UI state yet. The compositor
  instead reflects the **persisted** profile/recipe via
  `GET /api/strategy/profile` (loaded on mount/session change), which is
  correct but not live-during-the-turn.
* `from_run`'s `inputs` is a generic `["task description"]` placeholder:
  the run log only carries the assistant side of the turn (tool events +
  status), not the original user request, so it cannot be reconstructed
  from that file alone.
* `_run_log_path` in `src/recipes.py` reimplements `src/agent_runs.py`'s
  documented `DATA_DIR/runs/<session>.jsonl` shape rather than importing
  its private `_log_path` — a read of a stable, documented path, not a call
  into that module's runtime; if that shape ever changes this is the other
  place to update.
* `models_hint`/hardware-awareness in `choose_strategy` are soft,
  best-effort reads of `src/model_router.py::get_router_config` and
  `src/vram_admission.py::pending` — informational reasons only, never a
  gate on whether a strategy can be chosen.
