---
name: e2e-testing
description: Structure browser end-to-end tests around user journeys with a Page Object layer, and know how to tell a genuinely flaky test from a real regression. Use when writing or triaging tests that drive a real browser or a full running application rather than a single function.
version: 1.0.0
category: testing
tags: [e2e, testing, browser, playwright]
status: published
source: imported
---

## When to Use

Writing a test that exercises a full user journey through a UI (login,
checkout, a multi-step form), or triaging an existing end-to-end suite that
has started failing intermittently.

## Procedure

1. **Organise by user journey, not by page.** A test file named for what a
   user is trying to accomplish ("complete-checkout") ages better than one
   named for a single page, because most real journeys cross several pages.
2. **Use a Page Object (or equivalent) layer** that hides selectors and
   low-level interactions behind named methods (`loginPage.submit(email,
   password)`), so a selector change is a one-file fix instead of a
   find-and-replace across every test.
3. **Prefer role/text/test-id selectors over CSS classes or DOM structure.**
   A selector tied to visual styling breaks the moment a designer changes a
   class name for reasons that have nothing to do with the test.
4. **Wait for state, never for a fixed delay.** Assert on the element or
   condition that indicates the page is ready, rather than sleeping a fixed
   number of milliseconds — fixed sleeps are both slower than necessary on
   a fast run and still too short on a slow one.
5. **Isolate test data per run** (a fresh account, a scoped fixture) so
   tests can run in parallel and a failure in one doesn't leave state that
   breaks the next run.
6. **Capture a screenshot and trace on failure**, not on every run — enough
   evidence to diagnose without bloating every CI run with artifacts nobody
   will look at.
7. **Triage a flaky test by reproducing it in isolation** several times
   before deciding it's flaky rather than a real intermittent bug. If it is
   genuinely racy (a timing-dependent assertion, an unawaited async
   operation), fix the race; only quarantine with an open follow-up ticket
   if the fix isn't immediate — an unquarantined flaky test that "usually
   passes" trains everyone to ignore CI red.

## Pitfalls

- Asserting on CSS classes or exact pixel layout, which breaks on every
  unrelated visual change and teaches the team to distrust the suite.
- Sharing mutable test data across tests that then run in parallel,
  producing failures that only reproduce under specific run orders.
- A fixed `sleep(2000)` standing in for a real wait condition — it hides a
  real race the day the environment is slower than usual.
- Treating a repeatedly-flaky test as permanently quarantined with no
  ticket to actually fix it — quarantine is a pause, not a resolution.
- Testing a critical money-moving or destructive flow (payment, deletion)
  against a real external service instead of a sandbox/mock, risking real
  side effects from a test run.

## Verification

- The suite passes when run in parallel and in isolation, not just in the
  one order it happened to be written in.
- A failure produces enough evidence (screenshot, trace, or log) to
  diagnose without re-running it interactively.
- A test that was marked flaky has an open ticket describing the suspected
  race, not just a skip annotation.
- No fixed-duration sleep stands in for a real readiness check anywhere in
  the changed tests.
