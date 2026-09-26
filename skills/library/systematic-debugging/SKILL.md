---
name: systematic-debugging
description: Root-cause a bug through a fixed sequence — read the failure, reproduce it, check recent changes, trace the bad value to its source, form one hypothesis, then fix — instead of guessing at patches. Use when facing any test failure, production bug, or unexpected behaviour, before proposing a fix, especially under time pressure or after an earlier fix didn't work.
version: 1.0.0
category: engineering
tags: [debugging, root-cause, engineering, third-party]
status: published
source: imported
---

## When to Use

Any technical failure: a test failure, a production bug, a performance
regression, a build or integration failure. Use it *especially* when under
time pressure, when "just one quick fix" seems obvious, or when a previous
fix already failed — those are exactly the moments guessing feels fastest
and costs the most. Even a bug that looks simple has a root cause; the
process is fast enough for simple bugs that skipping it never actually saves
time. Adapted from https://github.com/obra/superpowers (MIT, © 2025 Jesse
Vincent).

## Procedure

1. **Read the failure completely** before forming any theory. Full stack
   trace, exact error text, line numbers, exit codes — errors usually
   contain the answer, and skimming past them is the single most common way
   to waste an hour.
2. **Reproduce it reliably.** Exact steps, does it happen every time? If it
   won't reproduce, gather more data (logs, a wider input set) rather than
   guessing at a fix for something you can't yet trigger on demand.
3. **Check what actually changed.** `git diff`/`git log` against the last
   known-good state, new dependencies, config or environment differences.
   Most regressions trace to something that changed recently, not to a
   pre-existing latent bug suddenly manifesting.
4. **For a multi-component flow** (API → service → database, CI → build →
   sign), add a log line at each boundary and run it once to see *where*
   the value goes wrong before spending time on *why* — this collapses a
   wide search into a narrow one in a single pass.
5. **Trace backward from the symptom.** Where does the bad value first
   appear? What called that with the bad value? Keep walking up the call
   chain until you find the actual origin, and fix there — a fix applied
   where the bug is *noticed* instead of where it *originates* is a symptom
   patch that will resurface elsewhere.
6. **Find a working analog.** Locate similar code in the same codebase that
   works, and diff it against the broken path line by line. Every
   difference is a candidate cause — don't dismiss one as "that can't
   matter" without checking.
7. **Form exactly one hypothesis**, stated concretely: "X is the root cause
   because Y." Test it with the smallest possible change, one variable at a
   time. If it's wrong, form a new hypothesis — don't stack a second guess
   on top of the first.
8. **Write a failing test that reproduces the bug** before implementing the
   fix (see `tdd-workflow`). This is what proves the bug is understood, not
   just patched around, and it becomes the permanent regression guard.
9. **Implement the smallest fix for the confirmed root cause.** No bundled
   refactor, no "while I'm here" cleanup — a fix mixed with unrelated
   changes is harder to verify and harder to revert if wrong.
10. **Count failed attempts.** If a fix doesn't resolve the issue, that's
    one failed hypothesis — return to step 7 with new information. After
    three failed fixes in a row, stop patching entirely: that pattern (each
    fix reveals a new problem somewhere else, or needs "just a bit more"
    refactoring) means the underlying design is wrong, not that the fourth
    guess will land. Raise the architecture question explicitly instead of
    trying again.

## Pitfalls

- Proposing a fix from a plausible-looking symptom without confirming the
  root cause — "it's probably X" is a guess dressed as a diagnosis.
- Changing more than one thing per attempt, which makes it impossible to
  tell which change (if any) actually helped, or introduces a second,
  unrelated bug alongside the fix.
- Treating a flaky failure as "rerun until green" instead of what it
  actually is: a race condition or an under-specified test that needs
  investigating, not silencing.
- Writing the fix and its regression test in the same pass without ever
  watching the test fail — that proves the test *can* pass, not that it
  *would have caught* the original bug.
- Attempting a fourth or fifth fix on the same architecture instead of
  stepping back once three have failed in a row — more attempts on a wrong
  foundation just cost more time, they don't converge.
- Skipping evidence-gathering in a multi-component system and guessing
  which layer is at fault instead of instrumenting each boundary once.

## Verification

- The root cause is stated in one concrete sentence, backed by evidence
  (a log line, a diff, a trace) — not "seems like" or "probably."
- A regression test exists that fails without the fix applied and passes
  once it's restored (temporarily revert the fix locally to confirm).
- The narrowest test suite that exercises the changed area was re-run and
  nothing else broke.
- If three or more fixes were attempted before this one, the architecture
  question was explicitly raised rather than silently trying a fourth
  patch.
