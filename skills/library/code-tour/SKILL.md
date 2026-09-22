---
name: code-tour
description: Write a short, ordered walkthrough of a specific change or subsystem — a handful of concrete stops with a one-line reason for each — instead of a wall of prose explaining the whole area. Use when someone needs to understand a change or a subsystem well enough to review or extend it, not just to know that it exists.
version: 1.0.0
category: writing
tags: [documentation, walkthrough, review]
status: published
source: imported
---

## When to Use

Handing off a non-trivial change for review, explaining an unfamiliar
subsystem to someone about to work in it, or documenting the "why" behind a
design decision that isn't obvious from the code alone. Not needed for a
one-file change that speaks for itself.

## Procedure

1. **Discover the shape of the thing you're touring first.** Skim the
   files involved and identify the two or three moments that actually matter
   — the interesting decision, the tricky edge case, the place a future
   reader would get confused without a pointer.
2. **Infer the reader.** A tour for the original author reviewing their own
   change a week later reads differently from one for someone touching this
   code for the first time. Decide which one you're writing before you start
   — it changes how much background each stop needs.
3. **Verify every anchor before writing it down.** A stop that points at
   "the validation logic around line 40" has to actually be there; re-read
   the exact lines you're about to reference so the tour doesn't go stale
   the moment it's written.
4. **Order the stops as a narrative, not a file listing.** Follow the flow
   of execution or the flow of a decision, not alphabetical file order. A
   tour that jumps from `models.py` to `views.py` and back because that's
   the call order is more useful than one grouped by directory.
5. **Write each stop as: where, what to notice, why it matters** — one to
   three sentences. "This function does X" is not a tour stop; "this
   function does X, and the reason it doesn't Y here is Z" is.
6. **Close with what's NOT covered.** Naming what you deliberately skipped
   (a related module, an edge case handled elsewhere) is as useful as the
   stops themselves — it stops the reader assuming silence means "nothing
   to see".

## Pitfalls

- Writing a tour that summarises what the code does line by line instead of
  pointing at the two or three places a reader would actually get stuck —
  volume is not depth.
- Referencing a line number without re-checking it; a tour written against
  yesterday's version of the file is worse than no tour, because it's
  actively wrong with the appearance of authority.
- Explaining the obvious (a getter, a simple loop) and rushing past the one
  genuinely subtle decision the tour exists to surface.
- Writing for an imagined reader who already knows everything the tour is
  supposedly explaining — check the "why" is actually spelled out, not just
  implied.

## Verification

- Every file/line reference in the tour was re-read at write time and
  matches the current content.
- A reader who follows the stops in order ends up at the same understanding
  the tour claims to deliver — read it back once, cold, as if you were not
  the author.
- The tour states what it does not cover, so silence is never mistaken for
  completeness.
