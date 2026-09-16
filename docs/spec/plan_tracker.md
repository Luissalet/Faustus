# P1 — plan tracker

`src/plan_tracker.py`. Compensates for what
`scratchpad/harness_wave/silhouettes_analysis.md` §2/§4 shows across 24
Silhouettes chats: a plan attachment (7/66/172 KB, inlined by
`build_user_content` as `=== File: ... ===`/`=== ZIP archive: ... ===`
after the user's own text) reinjected in full across 6+ new chats with no
state between them; the local model (Qwen 27B) answered "Reference context
received." with zero tool calls because nothing told it a plan with
per-task state already existed to query instead.

## What it does

1. `find_plan_attachment(text)` — first inlined attachment whose body is
   >= `agent_plan_tracker_min_chars` (default 3000) chars AND parses to
   >= 3 tasks; `None` for a short/unstructured attachment.
2. `parse_plan(title, body) -> PlanSpec` — sections by markdown headings
   (`#`-`####`), else numbered lists (`1.`/`1)`), else `- [ ]` checkboxes,
   in that priority; never raises. Each `PlanTask`: stable `id` (`t01`…),
   plan-native `key` when detectable (`WP03`/`Tarea 7`/`Fase 2`), full
   section `text`, `acceptance` lines, `files` (via
   `src.agent_harness.extract_path_tokens`), `depends_on` keys.
3. Persists at `DATA_DIR/plan_tracker/<scope>/<hash>.json` — never the
   user's repo. `scope` = `project_id`, or sha1[:12] of the normalized
   (Windows-safe) `workspace`. Holds the full spec (incl. each task's
   `text` — the only copy readable without reinjecting the attachment)
   plus per-task `state` (`pending|in_progress|done|skipped`, `evidence`,
   `updated_at`, `turn`) and `created_at`/`last_seen_at`/`seen_count`.
   `upsert_from_attachment` bumps `seen_count`/`last_seen_at` on a repeat
   (same hash) without re-parsing.
4. `brief(tracker, language, max_chars=2500)` — deterministic system block:
   progress, current task (acceptance/files), next 3 pending, and a fixed
   pointer to `plan_status`/`plan_task`/`plan_done`. `replace_attachment(text,
   tracker, max_task_chars=6000)` = `user_authored_text(text)` + `brief(...)`
   + only the current task's text — the rest of a 172 KB attachment never
   reaches the prompt.
5. `looks_like_execute_request(user_text)` — imperative-execution message
   ("Sigue implementando el plan"/"Finish") vs. context-only, by a
   word-bounded ES/EN verb list plus a length/no-`?` guard.

## Tools (`src/agent_tools/plan_tools.py`)

`plan_status` (progress + compact list), `plan_task` (id/key or current
task, full text/acceptance/files), `plan_done` (id + evidence >= 20 chars;
reports `unverified_files` from this turn's `ledger_mutations`),
`plan_skip` (id + reason), `plan_next` (advances to, marks `in_progress`,
the next pending task; does not close the current one). Scope from
`ctx["project_id"]`/`get_active_workspace()`, same as `todowrite`. No
active plan is a normal tool error, not an exception; classified with
`todowrite`/`ask_user` in `src/tool_capabilities.py` (no workspace/
external effect).

## Settings / wiring / tests

`agent_plan_tracker` (bool, `True`), `agent_plan_tracker_min_chars` (int,
`3000`), `agent_plan_tracker_task_chars` (int, `6000`). `src/agent_loop.py`/
`TurnLedger.check_completion` untouched here — see
`scratchpad/harness_wave/P1_wiring.md` for the integration diff. Tests:
`tests/test_plan_tracker.py`.
