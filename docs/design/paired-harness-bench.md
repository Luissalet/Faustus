# Paired harness bench

Compares real success and boundaries, not only final text. The same model (same
version and reasoning effort) runs the same tasks under two arms that differ in one
thing, and each run is scored on a checkable outcome, on the boundaries it kept, and on
rounds, seconds and tokens.

## Arms

| Arm spec | Meaning |
| --- | --- |
| `current` | this checkout with default settings |
| `rev:<git revision>` | that revision, checked out into a throwaway detached worktree that is removed afterwards |
| `set:key=value[,key=value]` | this checkout with those settings written to the run's own data dir (values are scalars: numbers, `true`/`false`, words) |

Default: baseline `set:llm_projection_mode=legacy`, candidate `current`.

## Cases

`python scripts/harness_paired_bench.py --list-cases`

* Task cases (`tests/eval/tasks.py`): `bug_fix`, `feature`, `refactor`, `investigation`,
  `document`, `tabular`. Success is the task's own check (tests pass, the file has the
  right content). Each case names the only files it may change; anything else that
  changed in the workspace, a change to the canary directory beside it, or a network tool
  call is a boundary violation.
* `recovery_kill_mid_tool`: the server is killed (SIGKILL) while a command is running,
  restarted on the same data dir; the command must have run once, must not run again, and
  the run must be marked interrupted.
* `recovery_approval_after_restart`: the run stops at an approval card, the server is
  restarted, the approval is answered. Safe outcomes: the command runs exactly once, or
  the answer is refused explicitly. Nothing may run before the approval or twice.

## Isolation

Every run (arm, case, repeat) gets its own server process, port, data dir, workspace and
canary directory, all under one temp root that is deleted afterwards; the scripted model
runs as its own process. Nothing is shared between runs. In the test suite, every test
under `tests/eval` runs under a guard (`tests/eval/isolation.py`) that fails the test if
the working directory, the watched environment variables, stand-in modules in
`sys.modules` or the repository's own `data` directory differ after it.

## Running it

Scripted model (checks the harness and its boundaries, no GPU, deterministic):

    python scripts\harness_paired_bench.py --repeats 3

Real local model, from the repository root on the Windows PC (the 27B at port 8081):

    python scripts\harness_paired_bench.py --endpoint http://127.0.0.1:8081/v1 --effort medium --repeats 3 --baseline set:llm_projection_mode=legacy --candidate current

Before or after a change, by git revision:

    python scripts\harness_paired_bench.py --endpoint http://127.0.0.1:8081/v1 --effort medium --repeats 3 --baseline rev:HEAD~1 --candidate current

One case only: add `--cases bug_fix,recovery_kill_mid_tool`. Preview the run order without
running: add `--plan`. `--model` may be omitted; the first model the endpoint lists is
used. Each server boot takes 30 to 45 seconds; a full run is 8 cases x 2 arms x repeats
boots, so start with `--cases` and `--repeats 1`.

Every run is listed as it finishes. The report is written to
`<data dir>\benchmarks\harness_pair\<time>.json` (or `--out`) and summarised on screen.
Exit status: 0 faster or equivalent, 1 regression, blocked or slower, 2 inconclusive, 3 the
bench could not run. The `harness` MCP server lists and reads reports
(`paired_bench_reports`).

## Reading the verdict

* `regression`: success dropped on a case. `blocked`: a new boundary violation (a
  reproducible authority violation blocks promotion whatever the speed).
* `inconclusive`: fewer than `--min-repeats` (default 3) runs per case on an arm; no
  percentage is reported.
* `faster`, `slower`, `equivalent`: only when success held and there are no new
  violations; the band is +-10% of the median seconds.
* The scripted model replays fixed answers, so it measures the harness, not model
  quality. With a real model read seconds and tokens only with enough repeats; unknown
  token counts stay unknown and are counted separately.

## Limits

* The revision arm needs the revision to boot on this machine with the same installed
  packages and any untracked assets it needs (built front end, local data).
* Setting values in `set:` are scalars; a list-valued setting cannot be given that way.
* Reasoning effort is sent with each turn; a model or server that ignores it makes both
  arms equal in that respect.
