# Night shift

An unattended queue of `dispatch` jobs run sequentially under a budget, with
a Markdown morning report. Module: `src/night_shift.py`.

## Shift shape

```json
{
  "id": "a1b2c3d4e5f6",
  "owner": "admin",
  "workspace": "/home/user/project",
  "tasks": ["fix the failing test in test_foo.py", "add a changelog entry"],
  "budget": {"max_minutes": 120, "max_tasks": 8, "max_tokens": null},
  "model": null,
  "verify": true,
  "started": 1700000000.0,
  "finished": 1700007000.0,
  "state": "done",
  "results": [{"task": "...", "job_id": "...", "status": "done", "verdict": "...",
               "files_changed": ["..."], "verification": {"...": "..."}}],
  "skipped": []
}
```

`state`: `queued` | `running` | `done` | `stopped` | `budget_exhausted` | `error`.

Each task in `tasks` (up to 12) runs as its own `dispatch` job
(`verify: "auto"` by default, `fix_rounds: 1`), one after another. Between
tasks the budget is checked: elapsed minutes since the shift started, tasks
already run, and (if `max_tokens` is set) tokens spent so far
(`dispatch.compact(job)["result"]["totals"]`). The first ceiling reached
stops the queue with `state: "budget_exhausted"`; the task in flight is
always allowed to finish. `stop()` sets a flag checked the same way, so a
shift stops after its current task rather than mid-task.

## Settings

- `night_shift_default_max_minutes` (120)
- `night_shift_default_max_tasks` (8)

## API (admin-only, same trust class as `/api/dispatch`)

- `POST /api/night-shift` -- body `{tasks, workspace, budget?, model?, verify?}`
  -> the queued shift.
- `GET /api/night-shift` -- recent shifts for this owner.
- `GET /api/night-shift/{id}` -- one shift's current state and results.
- `POST /api/night-shift/{id}/stop` -- ask a running shift to stop after its
  current task.
- `GET /api/night-shift/{id}/report` -- the Markdown report (`text/markdown`).

## Agent tool

`night_shift` (`src/agent_tools/night_shift_tools.py`), actions `start`,
`status`, `stop`, `report` -- same admin-only, `ToolEffect.EXECUTE_CODE`
class as `delegate_agents`.

## Home card

Home cards are read live off a `ScheduledTask` (`src/home_cards.py`), so a
night shift gets one through the `night_shift_report` watch action
(`src/watchers.py`): schedule it (daily, morning) and pin the resulting
task -- it reports the latest finished shift's Markdown report, once per
shift (`always: true` to report every run regardless).
