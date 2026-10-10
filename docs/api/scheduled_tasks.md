# Scheduled tasks: run window, period key, pause and closed statuses

`/api/tasks` (routes in `routes/task/task_routes.py`), the scheduler in
`src/task_scheduler.py` and the pure rules in `src/task_windows.py`.
A task that declares none of the fields below behaves exactly as it always did.

## What a scheduled task can declare

| Field | Where | Meaning |
|---|---|---|
| `window_minutes` | create / update / `manage_tasks` | Minutes after the scheduled moment during which a run is still wanted. A catch-up that reaches the task later is **not** run late: it is recorded as `skipped-window` and the schedule moves to its next slot. 1 to 43200. |
| `period_key` | create / update / `manage_tasks` | `day`, `iso_week` or `month`, in the task's own time zone (UTC for a legacy task with none). At most one run per period; a second or late firing in a period that already ran is recorded as `skipped-period`. |
| pause | `POST /api/tasks/{id}/pause`, `/resume`, `manage_tasks` pause/resume | `status="paused"` (read back as `paused: true`). A paused task never runs. |

Both guards apply to `trigger_type="schedule"` only (a window or period on an
event or webhook task is a 400). On `PUT`, a field left out is kept and an
explicit `null` (or `0` for the window) clears it.

## How the window is measured

From the instant the occurrence was **first** due, not from its last
postponement. When the foreground is busy, a quiet window defers it, or the
startup sweep pushes an overdue task 60 s ahead, the scheduler remembers the
original due time (valid only for the postponed `next_run`), so a task cannot
be kept waiting past its window. The startup sweep judges the window against
the moment the task will really run (sweep time plus 120 s of grace).

## How the period is used up

A period is used up when a run for it ends `ok`, `partial` or `failed`. A run
`blocked-login` leaves it open (a retry in the same period is allowed), and so
do aborted runs and runs deferred by a quiet window. A late run is filed under
the period it was **due** in: due 23:50 on the 9th and run at 00:20 on the
10th, it counts for the 9th. A person's "Run now" ignores both guards.

## Closed statuses

Every run row ends in one of: `ok`, `partial`, `failed`, `skipped-window`,
`skipped-period`, `skipped-paused`, `blocked-login`, with a one-line reason.
`GET /api/tasks/{id}/runs`, `GET /api/tasks/runs/recent` and the `manage_tasks`
`history` action return them as `closed_status` and `reason`.

The stored `TaskRun.status` keeps its historical word for every client that
already reads it; `closed_status` is derived from it:

| Stored `status` | `closed_status` |
|---|---|
| `success` | `ok` (`partial` if the row also carries an `error`) |
| `error` | `blocked-login` when the error reads like a rejected login or credential, otherwise `failed` |
| `aborted` | `failed` |
| `skipped` (a no-op) | `ok` |
| `skipped` with a paused task | `skipped-paused` |
| `fenced` (another worker owned the occurrence) | `skipped-period` |
| `skipped-window` / `skipped-period` / `skipped-paused` | as is (new rows written by the scheduler) |
| `queued` / `running` | `null` (still open) |

Skips are real rows with `started_at == finished_at`; they do not touch
`last_run` or `run_count`.

Where each skip is written:

* `skipped-window`: in the due scan (after the dispatch lease is won, so a
  worker that lost the claim cannot record it) and in the startup sweep.
* `skipped-period`: in the due scan, same place.
* `skipped-paused`: when a run is requested for a paused task (a manual run, a
  firing that was already queued); and, for a paused task that declared a
  window or period, once per slot that comes due while it is paused (the
  schedule then moves to the next slot). A paused task with no guard writes
  nothing new.

## API additions

`GET /api/tasks`, `GET /api/tasks/{id}`, `POST`, `PUT` return, on every task:
`window_minutes`, `period_key`, `paused`, and (when the task has runs)
`last_run_closed_status`, `last_run_reason`, `last_run_at` (the newest run,
skips included; `last_run` itself still means the last run that ran).

## Agent tool (`manage_tasks`)

`create` and `edit` accept `window_minutes` and `period_key`; `list` shows
them and the last closed status; the new `history` action (`task_id`, optional
`limit`, default 10, max 50) returns recent runs with `closed_status` and
`reason`. An invalid value is an error before anything is written.

## Storage

Two tables in `task_policies.db` (next to `task_policies`, whose
`get_task_policy` keeps returning exactly the keys it always did):
`task_schedule_guard` (task id, window, period key, original due time) and
`task_period_log` (task id, period value, closed status, run id; the primary
key is what stops two workers using a period twice). Helpers:
`set_task_schedule_guard` / `get_task_schedule_guard` in `src/task_scheduler.py`.

## The night shift

`src/night_shift.py` is not part of this: it is a job queue started by hand or
by one scheduled task, with its own budget and its own states (see
`night_shift.md`). Scheduling a night shift goes through a normal scheduled
task, so the guards above apply to that task.
