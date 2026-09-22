---
name: tdd-workflow
description: Write the failing test before the code that makes it pass, then refactor with the test as a safety net. Use when implementing a new feature, fixing a bug, or changing behaviour in code that has (or should have) automated tests.
version: 1.0.0
category: testing
tags: [tdd, testing, workflow, verification]
status: published
source: imported
---

## When to Use

Any task that changes runtime behaviour: a new endpoint, a bug fix, a
refactor that must not change output. Skip it for pure prose, config-only
edits, or a throwaway exploration script — TDD earns its cost on code that
will be run again.

## Procedure

1. **Detect the test runner first.** Look for `pytest.ini` / `pyproject.toml`
   `[tool.pytest]`, `package.json` `scripts.test`, `go test`, `cargo test`,
   or a `Makefile` target, in that order. Never guess a command; run it once
   on the untouched suite to confirm it passes before you change anything —
   a suite that was already red tells you nothing about your change.
2. **Write the test first, and watch it fail for the right reason.** State
   the behaviour as an assertion before writing the implementation. Run it
   and read the failure: it should fail because the feature doesn't exist
   yet, not because of a typo in the test itself.
3. **Write the smallest implementation that makes it pass.** Resist adding
   behaviour the test doesn't ask for — extra scope now is untested scope.
4. **Run the test again and confirm green**, then run the surrounding test
   file (not necessarily the whole suite) to catch a regression next door.
5. **Refactor with the green test as your net.** Clean up naming,
   duplication, and structure; re-run the test after each meaningful change.
6. **Repeat** for the next behaviour, one assertion at a time rather than
   writing five tests up front and making them all pass at once — a single
   failing assertion is much easier to read than a wall of red.
7. **Close with evidence, not a claim.** Report which command you ran, its
   exit code, and — for a bug fix — that the test would have caught the
   original bug (comment out the fix locally, watch it fail, then restore
   it) before saying the task is done.

## Pitfalls

- Writing the implementation and the test in the same pass and never
  watching the test fail — you have then proven the test can pass, not that
  it can fail. A test that has never been red is unverified.
- Chasing coverage percentage instead of behaviour: a suite that hits every
  line but never asserts an edge case (empty input, a second call, a
  concurrent write) is decoration, not a safety net.
- Testing the mock instead of the code: a test that patches out everything
  the function does verifies the patches were called, not that the feature
  works. Prefer exercising the real function against a small real fixture
  when the cost is low enough.
- Running the entire suite after every one-line change. It's slow enough
  that people stop doing it; run the narrow file during the loop and the
  full suite once before reporting done.
- Treating a flaky test as "probably fine, rerun it" — a test that fails
  1 time in 10 is either racy or under-specified, and quarantining it
  without a follow-up ticket just moves the bug from "visible" to "silent".

## Verification

- The specific test file for the change is green, and was red before the
  implementation existed (you saw it fail, not just "it should have").
- The narrowest command that exercises the changed module was run, its
  output (or exit code) is quoted in the report, not paraphrased.
- For a bug fix: the test fails again when the fix is reverted locally, and
  passes once it's restored — proof the test actually covers the bug.
- No unrelated test in the same file or package started failing.
