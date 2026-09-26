---
name: code-simplifier
description: Simplify code just written or touched in this session for clarity and consistency without changing what it does — collapsing nested ternaries, deleting redundant abstractions, and choosing explicit code over compact code. Use when a feature or fix is finished and about to be reported done, on the code actually modified.
version: 1.0.0
category: engineering
tags: [refactoring, readability, code-quality, third-party]
status: published
source: imported
---

## When to Use

After finishing a feature or bug fix, on the specific code just written or
modified — not a license to restructure code outside the current change,
and not a substitute for a full `code-review-checklist` pass. Skip it for
a one-line, already-clear fix; there's nothing to simplify. Adapted from
https://github.com/getsentry/skills (Apache-2.0, © 2025 Functional
Software, Inc. dba Sentry).

## Procedure

1. **Scope to what changed.** Only the functions and files touched in this
   session, unless the user explicitly asked for a broader pass —
   simplifying untouched code turns a small, reviewable diff into a large
   one nobody asked for.
2. **Re-read each changed block with one question:** could someone
   unfamiliar with why it was written understand it without an
   explanation? Anywhere the answer is no is the simplification target,
   not the whole file.
3. **Collapse nested ternaries and deep conditional chains** into an
   if/else chain or a small dispatch table — whichever names the branches
   most clearly. A chain of `a ? b : c ? d : e` reads as a puzzle; a
   sequence of `if` statements with early returns reads as a list.
4. **Break up overly compact one-liners** (a chained filter/map/reduce, a
   dense comprehension) into named intermediate steps when a reader would
   otherwise have to mentally unwind it to know what it produces.
5. **Delete a wrapper that adds a name but no behavior** beyond what its
   one call site already says directly — a one-line `isNotEmpty(x)` used
   once, wrapping `x.length > 0`, is indirection without payoff.
6. **Remove comments that just restate the line below them**; keep only
   the ones explaining a non-obvious "why" a reader couldn't derive from
   the code itself.
7. **Follow the project's own established conventions** — its lint config,
   style guide, or dominant existing pattern — rather than a generic
   preference. Consistency with the surrounding codebase outranks any
   personal style choice.
8. **Re-verify behavior after each simplification.** Re-run the tests that
   cover the changed code, or, if none exist, re-read the diff line by
   line against the original logic. A simplification that changes output
   is a bug wearing a cleanup's clothes.
9. **Stop before over-simplifying.** Don't merge genuinely distinct
   concerns into one function to save lines, don't remove an abstraction
   already used by three or more call sites just because it looks
   unnecessary from the one you're reading, and don't trade clarity for a
   shorter diff.

## Pitfalls

- Simplifying code you didn't touch this session "while you're in there" —
  scope creep turns a small, reviewable diff into one that's hard to
  review and easy to introduce an unrelated regression into.
- Collapsing a branch that handles a real edge case because it "looks
  redundant," without first checking why it was added.
- Optimizing for fewer lines instead of fewer concepts — a dense one-liner
  that saves two lines but costs the next reader thirty seconds to unwind
  is not simpler, it's shorter.
- Removing an abstraction used by multiple call sites because it looks
  unnecessary from the single one currently in view.
- Skipping the re-run/re-read verification step and assuming a rewrite
  preserves behavior because it "should."

## Verification

- The changed code is functionally identical before and after: tests still
  pass, or the diff was read end to end against the original logic.
- Every nested ternary, chained one-liner, or dead abstraction touched this
  session was either simplified or has a stated reason it was left as is.
- The result follows the project's own conventions (lint rules, dominant
  patterns), not an imported generic style.
- No code outside the current session's changes was touched.
