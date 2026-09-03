---
name: faustus-workers
description: Offload mechanical coding/file work to Faustus's local workers (Ollama on the user's PC) through the faustus-workers MCP server, so the expensive model only plans and reviews. Use whenever a task means editing files, running tests or builds, refactoring to a clear spec, searching a codebase or producing boilerplate in a folder on the user's machine — and the faustus-workers tools (dispatch_workers, workers_wait…) are available.
---

# Faustus workers

The user runs Faustus (their local AI workstation) with the `faustus-workers`
MCP server. Its tools hand work to LOCAL models on their machine; you keep the
plan, the ordering and the review. Their Claude usage is the scarce resource:
do not read files or run tests yourself when a worker can.

## Procedure

1. Call `workers_guide` once per session (it is short) if you have not yet.
2. Turn the user's request into 1–4 self-contained tasks. Each names the
   files, the exact behaviour and the command that proves it (`pytest -q`,
   `npm test`, …). Independent tasks → `parallel: true`; dependent ones in
   order (`parallel: false`) or as separate jobs. Always pass `workspace`
   (the absolute folder).
3. `dispatch_workers` → `workers_wait` (up to 600 s). While waiting, do not
   guess at the outcome.
4. Read the compact result: trust `changed:` and the static checks over the
   worker's prose. A `stalled` / `timeout` worker did part of the work — read
   what changed and dispatch the remainder as a narrower task.
5. Answer the user with what changed, what was verified, and the board link
   (`board:`) for the details. Say plainly which parts came from the workers.

## Do not
- Paste file contents into a task that the worker can read itself.
- Send two tasks that edit the same file in parallel.
- Redo a worker's job by hand; narrow the task and dispatch again.
- Dispatch design decisions, ambiguous requests, or anything the user must
  decide.
