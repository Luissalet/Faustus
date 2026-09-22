---
name: team-agent-orchestration
description: Split a large task into independent, non-overlapping sub-tasks and run them as parallel sub-agents with `delegate_agents`, then verify each worker's report against evidence rather than trusting its own summary. Use when a task is large enough to parallelize safely — several independent areas of a codebase, or several independent research threads — and small enough per sub-task that one worker can finish it without further delegation.
version: 1.0.0
category: planning
tags: [orchestration, delegation, agents]
status: published
source: imported
---

## When to Use

A task that decomposes into genuinely independent pieces — different files
or modules that don't share state, or separate research questions — where
running them one after another would just be slower for no benefit. Not for
a task that's actually one continuous thread of reasoning; splitting that
produces workers stepping on each other or duplicating context they each
had to rebuild independently.

## Operating Model

This app runs delegated work through `delegate_agents`: each sub-task
becomes its own worker in its own child session (visible afterwards in the
Agents folder, so the work is auditable), bounded by a per-worker round and
time budget, and reporting back through the coordinator rather than talking
to each other directly. Up to a handful can run concurrently; a local model
backend usually serves one generation at a time, so "parallel" mainly buys
overlapped tool time (I/O, waiting on external calls), not literally
simultaneous generation.

## Procedure

1. **Split by files, not by feeling.** Two workers must never be asked to
   write the same file — give each task an explicit list of the files it
   owns. A task with no declared files defaults to claiming what it writes
   first, which works for one worker and produces a collision for two.
2. **Write each task as self-contained.** A worker starts a fresh session
   with no memory of the coordinator's exploration — state the goal, the
   relevant context already discovered, the files it owns, and what "done"
   looks like, explicitly. A task that assumes shared context the worker
   never received will improvise something plausible and wrong.
3. **Keep tasks narrow enough for one pass.** A worker that discovers its
   task actually needs to touch a file it doesn't own should report that and
   stop, not silently expand its claim — the coordinator re-plans, the
   worker doesn't freelance into someone else's territory.
4. **Assign a reviewer for anything risky**, run after the workers finish,
   with read-only access — reviewing the combined diff catches the failure
   mode two independent workers can't see themselves: each one's piece
   looking fine in isolation while the seam between them is wrong.
5. **Verify from evidence, not from the worker's own narrative.** A report
   that says "implemented and tested" is a claim; the files it actually
   changed, the commands it actually ran, and their actual output are
   evidence. Trust the latter.
6. **Re-plan rather than push through** when a worker reports a blocker or a
   scope mismatch — the coordinator has the full picture; a worker
   improvising past its own boundary is exactly the failure this whole
   pattern exists to avoid.

## Pitfalls

- Two tasks that both need to touch the same shared file (a router
  registration list, a shared constants module) without one being given
  clear ownership — the classic collision, and it's a planning failure, not
  a bad-luck one.
- A task description so thin the worker has to guess at context the
  coordinator already had and simply didn't write down.
- Trusting a worker's self-reported "all tests pass" without the actual
  command output — a worker under time pressure describes what it intended,
  not always what happened.
- Delegating something that was actually one continuous train of thought,
  producing two workers who each rebuild half of the same context
  independently at twice the cost.
- Skipping the reviewer pass on a multi-worker change that touches a risky
  area, because each individual diff looked fine on its own.

## Verification

- No two tasks in the same job claim the same file.
- Every worker's report is checked against real evidence (files changed,
  command output) before being taken as fact.
- A risky multi-worker change went through a read-only review of the
  combined diff, not just each worker's own piece.
- A worker that hit a scope mismatch was re-planned around, not left to
  improvise past its boundary.
