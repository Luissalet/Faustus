# H5 — test debt journal (`src/test_debt.py`)

## Problem

`project_tests.py::compare_with_baseline` already separates a NEW test
failure (caused by this turn) from one that was `pre_existing` (failed at
the checkpoint too), and narrows further to `exempt` (pre-existing, and not
tied by name to the files this turn changed). That comparison is purely
per-turn: nothing remembers that the *same* test has been exempt across many
turns. The silhouettes forensic analysis is the concrete case:
`test_real_end_to_end[star]` (IoU 0.9755 < 0.98) went red on 14-09 (chat
`8ee5881b`), was still "pre-existing" on 15-09 (`9ca3273d`), and again on
16-09 (`d20e933f`/`782b7d89`) — three full days where a real regression was
simply invisible, because "exempt" reads as "forgiven".

## Registry

Persistent, per-project, at `DATA_DIR/test_debt/<project_hash>.json`
(`project_hash = sha256(project_id)[:24]`, filesystem-safe for any project
id). One entry per test id (`path::nodeid`, the same form
`project_tests._failure_id` produces):

```json
{"test_id": "...", "first_seen": "...", "last_seen": "...", "turns_seen": 3,
 "last_turn": "...", "dismissed": false, "dismiss_reason": null, "dismissed_at": null}
```

## API

- `record(project_id, tests_result, *, turn=None) -> [entries]` — folds this
  turn's `tests_result["exempt"]` / `["pre_existing"]` test ids into the
  registry and persists it.
  - **Idempotent per turn**: passing the same `turn` value twice for a test
    id already recorded under it does not advance `turns_seen` again.
  - A test id no longer reported (fixed, or promoted to a real new failure)
    is **dropped** — unless it was `dismiss`ed, which is kept as quiet
    history. If a dismissed test id starts failing again under the exact
    same id, `record` adds it back as a *fresh* entry (`turns_seen` restarts
    at 1) rather than auto-forgiving it under the old dismissal.
- `overdue(project_id, *, turns=None) -> [entries]` — undismissed entries
  with `turns_seen >= agent_test_debt_turns` (default 3), most-seen first.
- `dismiss(project_id, test_id, reason) -> bool` — requires a non-blank
  `reason`; persists `dismissed=True` immediately, removing the entry from
  `overdue()`/`todo_items()` from that point on.
- `todo_items(project_id, *, turns=None, language="en") -> [todo dicts]` —
  one `{"id": "test_debt:<test_id>", "content", "content_es", "content_en",
  "status": "pending", "priority": "high", "source": "test_debt", ...}` per
  overdue entry. Deterministic `id`/`content` per (test_id, turns_seen) make
  repeated calls **idempotent**: merging the same debt item into a todo list
  twice, or once per turn while `turns_seen` is unchanged, never duplicates.
- `merge_todo_items(existing, debt_items) -> [todos]` — merges by `id` first,
  falling back to exact `content` for a todo list with no `id` field (the
  shape `coding_tools.TodoWriteTool` persists today).

## Non-goals

- Does not run tests or decide what is pre-existing — that stays
  `project_tests.py`'s job; this module only remembers its verdicts over
  time.
- Does not auto-fix or auto-skip anything. `dismiss` is an explicit,
  reasoned human/agent decision, never inferred.
- Does not call `project_tests`/`coding_tools`/`agent_loop` — kept
  dependency-free per CONTRATO.md rule 2; see `H45_wiring.md` for exactly
  where the integrator calls `record`/`todo_items`.
