---
name: performance-optimization
description: Measure before optimizing — establish a baseline, find the actual bottleneck with profiling or query-plan evidence, fix only what's proven to matter, then re-measure the same way before deciding to keep or revert. Use when a performance requirement exists, a regression is suspected, or profiling already points at a concrete bottleneck such as an N+1 query or an unbounded fetch.
version: 1.0.0
category: engineering
tags: [performance, profiling, optimization, third-party]
status: published
source: imported
---

## When to Use

A stated performance requirement (a latency budget, an SLA), a reported
regression, or profiling data that already points at a real bottleneck.
Don't reach for this on a hunch with no measurement behind it —
premature optimization adds complexity the codebase then has to maintain
forever for a gain nobody confirmed. Adapted from
https://github.com/addyosmani/agent-skills (MIT, © 2025 Addy Osmani).

## Procedure

1. **Require evidence before starting.** Only proceed with a stated
   budget, a reported regression, or profiling data — not "this feels
   slow."
2. **Establish a baseline** with the exact measurement you'll reuse
   afterward (a timed query log, a request-timing wrapper, a profiler
   run), under conditions you can reproduce later (same cache state, same
   data volume, same sample count).
3. **Use the symptom to pick what to measure first:** a slow first load
   points at bundle size or server response time; sluggish interaction
   points at main-thread work; one slow endpoint points at its own
   queries; *all* endpoints slow at once points at a shared resource
   (connection pool, CPU, memory, GC).
4. **For a slow query, read the actual query plan** (`EXPLAIN ANALYZE` or
   the equivalent) instead of guessing an index is missing. Look for a
   full/sequential scan where an index was expected, row estimates far off
   from reality (stale statistics), or a sort step the index doesn't
   cover. Index for the query's shape — equality columns before the
   range/sort column in a composite index — and re-run the plan afterward
   to confirm it actually changed.
5. **For a slow endpoint, check for an N+1 pattern** (one query per item
   inside a loop) and replace it with a single joined or batched query;
   check for an unbounded fetch and add pagination.
6. **For "every endpoint is slow at once," suspect a shared bottleneck
   first** — connection pool exhaustion, a GC pause, lock contention —
   rather than optimizing any single endpoint. Raising a pool's size in
   response to exhaustion, without finding what's holding the existing
   connections, usually just relocates the queue somewhere less visible
   (the database instead of the app).
7. **Change exactly one thing per measurement.** Several optimizations
   landed together produce one number that can't be attributed to any of
   them; if they must ship together, measure each in isolation first.
8. **Re-measure exactly like the baseline** — same command, same
   conditions, same sample size — and compare the delta against
   run-to-run variance, not just the raw before/after numbers. A change
   inside normal noise is not a win.
9. **Decide strictly:** past the improvement threshold with tests still
   green → keep it, with the before/after numbers in the commit message;
   inside noise, worse, or a test went red → revert. This includes
   "neutral" changes that don't hurt but don't measurably help either —
   every kept change is something to maintain forever, so it has to pay
   for itself.
10. **Log every attempt, kept or reverted**, with its baseline, result, and
    verdict (a `PERF.md` entry or a line in the PR description), so a
    dead idea isn't tried again unknowingly next quarter.
11. **Add a guard for the metric that mattered** — a CI performance budget
    for a reproducible regression, or a monitored p75/p95 in production for
    a field one — so a future regression on this path is caught
    automatically.

## Pitfalls

- Optimizing before measuring — "this is obviously slow" is a guess until a
  profiler or query plan confirms the actual bottleneck.
- Adding an index because a query "feels slow" without reading its plan —
  the index may already exist and be unusable, or the column may be too
  low-selectivity for one to help, and every index taxes every write
  regardless.
- Caching a call that was already cheap — it buys nothing and adds a
  staleness bug and an eviction policy to maintain for no measured gain.
- Raising a connection pool's size in response to exhaustion without first
  finding what's actually holding the connections.
- Shipping several optimizations together in one measurement, so a win (or
  a loss) can't be attributed to any single change.
- Keeping a change that landed "inside noise" because discarding it feels
  wasteful — it still costs maintenance forever for nothing measured back.
- Retrying an optimization that already failed, because nobody recorded
  that it was tried and reverted before.

## Verification

- A profiler, timing measurement, or query plan — not intuition —
  identified the bottleneck being addressed.
- Before/after numbers exist, measured the same way, and the improvement
  exceeds normal run-to-run variance.
- Tests are still green after the change; a win that required breaking or
  skipping a test is a regression, not an optimization.
- Any change that didn't beat the baseline was reverted, not kept as
  "neutral."
- The attempt — kept or reverted — is recorded somewhere so it won't be
  retried blind later.
