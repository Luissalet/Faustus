# Typed goals of a plan task (`plan_done`)

A task of the persisted plan (`src/plan_tracker.py`, tools `plan_status` /
`plan_task` / `plan_done` / `plan_skip` / `plan_next`) can declare **typed
goals** in the plan text. `plan_done` runs them before it seals the task, and
a failing goal keeps the task open. Module: `src/plan_goals.py`.

## Declaring goals

One goal per line, anywhere inside the task's section (a bullet, a checkbox,
backticks or bold around the kind are all fine):

```
test_passes: pytest tests/test_notes.py -q
http_ok: https://example.com/health 200
```

| Kind | Run by `plan_done`? | Spec |
|------|---------------------|------|
| `test_passes` | yes | the rest of the line is the test command |
| `http_ok` | yes | `<url> [status]`; without a status, 2xx/3xx passes |
| `file_exists`, `artifact_present`, `doc_revision_at_least`, `custom_check` | no: reported as `skipped` ("not checkable automatically") | free text |

Goals are parsed once with the plan (`PlanTask.goals`, tracker
`parser_version` 4) and listed by `plan_task`.

## What `plan_done` does

1. Same checks as before (task exists, evidence of at least 20 characters).
2. If the task has goals and `agent_plan_goals` is on, it evaluates them
   (off the event loop) and stores a compact report on the task's state
   (`state[task].goals`).
3. Any `failed` goal: **the task is not sealed**. The result has
   `sealed: false`, `exit_code: 1`, an `error` that lists each failure with its
   evidence, and `goals` (the full report). The task stays `pending` /
   `in_progress`; the model fixes the cause and calls `plan_done` again, which
   runs the goals again. No new round mechanism: it is the same
   "open task, keep working" loop the plan auto-continue already drives.
4. Nothing failed: the task is sealed (`sealed: true`) and the report is
   returned and kept. `skipped` goals are listed as "NOT verified" in the
   output; they never count as passed.
5. `plan_tracker.reconcile` (the end-of-turn auto-close from todos / written
   files) leaves a task alone while it has runnable goals that have not
   passed, so a todo cannot bypass them. `plan_skip` is unchanged.

## Report

```json
{"ok": false, "total": 2, "passed": 1, "failed": 1, "skipped": 0,
 "results": [
   {"id": "g1", "kind": "test_passes", "target": "pytest tests/test_notes.py -q",
    "status": "failed", "exit_code": 1, "duration_s": 0.9,
    "evidence": "exit 1 in 0.9s; 1 failed; last lines: FAILED tests/test_notes.py::test_ok ..."},
   {"id": "g2", "kind": "http_ok", "target": "https://example.com/health",
    "status": "passed", "status_code": 200, "evidence": "GET https://example.com/health -> 200 (expected 200) in 0.2s"}]}
```

`status` is `passed`, `failed` or `skipped`; a skipped goal also carries
`reason`.

## Execution rules

* **`test_passes`** goes through the harness test runner
  (`src.project_tests.run_tests`): an argv list (never a shell), the project's
  interpreter, the scrubbed environment, a process-tree kill on timeout and
  the shared `cpu_heavy` admission. The command must start with a recognised
  runner: `pytest`, `python -m pytest|unittest`, `npm|pnpm|yarn [run] test`,
  `node --test`, `cargo test`, `go test`, `make test`. Shell operators
  (`; & | < > ( )`), backticks, `$(...)` and newlines are refused. Every
  path-like argument (including `--opt=value`) must resolve inside the turn's
  workspace; absolute paths outside it, `..` escapes and drive-relative paths
  are refused. Use forward slashes on Windows.
* **`http_ok`** is one bounded GET through `src.outbound_fetch.fetch` under the
  `public_untrusted` profile, the same broker and profile `web_fetch` uses:
  public hosts only. Loopback, LAN, link-local and internal names are refused
  before any request.
* **Policy.** A goal runs only if the turn's `ToolPolicy` would let the agent
  make the equivalent call (`bash` for a test command, `web_fetch` for a URL).
  The read-only preset, a disabled tool, guide-only and MCP-only turns
  therefore skip the goal, as does a turn with no bound workspace and a
  required sandbox (`agent_sandbox_mode: required`: the host runner cannot
  honour it). An unreadable policy refuses.
* **Time.** Each goal is bounded by `agent_plan_goal_timeout_seconds`
  (default 120, clamped to 10-600 s); the whole call shares a 900 s budget,
  and goals left after it is spent are skipped.

`skipped` is explicit but does not block sealing: a policy refusal must not make
a task impossible to close. The response says which goals were not verified and
why.

## Settings

| Key | Default | Meaning |
|-----|---------|---------|
| `agent_plan_goals` | `true` | run typed goals in `plan_done`; off = behaviour as before |
| `agent_plan_goal_timeout_seconds` | `120` | per-goal limit (10-600) |

## Tests

`tests/test_plan_goals.py`: parsing, command resolution and confinement,
passing / failing / timed-out / budget-bounded test goals, `http_ok` with a
faked probe and the real SSRF preflight, policy / sandbox / workspace refusal,
unknown kinds, `plan_done` sealing and not sealing, `reconcile`, and two runs
of a real `pytest` subprocess through the harness runner.
