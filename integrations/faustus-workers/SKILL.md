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
   order (`parallel: false`). `workspace` (the absolute folder) is required.
   Pass that proving command as `verify`: Faustus runs it ITSELF after the
   workers (with `auto` it detects the project's test runner) and sends one
   fixer worker with the failure output when it fails (`fix_rounds`).
3. `dispatch_workers` → `workers_wait`. If the answer says *still running —
   call workers_wait again*, call it again; never re-dispatch the same task
   because a wait returned early. While waiting, do not guess at the outcome.
4. Read the compact result top-down: `verdict`, then `changed on disk`
   (what Faustus saw — a worker's `claims:` are its own word; `claimed but
   NOT changed` lists the claims that did not happen), then `verification:`
   (passed / FAILED with the failing tests / not run). `partial` means a
   worker ended `stalled` / `timeout` / `error` or the verification failed:
   read what changed and dispatch the remainder as a narrower task.
5. Answer the user with what changed, what Faustus verified (the command and
   its result), and the board link (`board:`) for the details. Say plainly
   which parts came from the workers.

## Do not
- Paste file contents into a task that the worker can read itself.
- Send two tasks that edit the same file in parallel.
- Redo a worker's job by hand; narrow the task and dispatch again.
- Dispatch design decisions, ambiguous requests, or anything the user must
  decide.
