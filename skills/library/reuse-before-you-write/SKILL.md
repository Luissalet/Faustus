---
name: reuse-before-you-write
description: Before writing new code, climb a fixed ladder — does this need to exist, does the codebase already have it, does the standard library, does an already-installed dependency, can it be one line — and stop at the first rung that holds. Use when writing new code, fixing a bug, refactoring, or choosing a dependency, to avoid reinventing something that already exists.
version: 1.0.0
category: engineering
tags: [simplicity, yagni, dependency-hygiene, third-party]
status: published
source: imported
---

## When to Use

Any coding task: writing new code, fixing a bug, refactoring, or deciding
whether to reach for a new dependency. Not for non-coding requests (prose,
research, translation). It runs *after* understanding the problem, never
instead of it — a fast, minimal fix in the wrong place is a second bug, not
a win. Adapted from https://github.com/DietrichGebert/ponytail (MIT,
© 2026 DietrichGebert).

## Procedure

1. **Understand the problem first.** Read the task and the code it
   touches, and trace the real flow end to end before picking a rung on
   the ladder below. The ladder is a reflex for *how much* to write, not a
   substitute for reading what's actually there.
2. **Rung 1 — does this need to exist at all?** A speculative requirement
   (an extension point nobody asked for, a config knob for a value that
   never changes) gets skipped, with the skip stated in one line. This
   rung is free and catches the most avoidable work.
3. **Rung 2 — is it already in this codebase?** Search for an existing
   helper, utility, type, or pattern (grep for similar function names,
   check neighboring modules) before writing anything. Re-implementing
   something that already lives a few files over is the most common source
   of duplicated logic.
4. **Rung 3 — does the standard library do it?** Reach for it before
   writing a custom version of something the language already ships.
5. **Rung 4 — does a native platform feature cover it?** An HTML input
   type over a picker library, a CSS rule over JavaScript, a database
   constraint over application-level validation.
6. **Rung 5 — does an already-installed dependency solve it?** Use what's
   already in the project before adding anything new. Every new dependency
   is a liability — maintenance, audit surface, bundle or install size —
   that has to earn its place against what a few lines of existing code
   could already do.
7. **Rung 6 — can it be one line?** If so, write one line.
8. **Rung 7 — only then, the minimum code that actually works.** Prefer
   deletion over addition, and a boring, explicit implementation over a
   clever one — clever is what someone has to decode at 3am.
9. **For a bug report, fix the root cause, not the named symptom.** A
   report names one broken path. Before editing, find every caller of the
   function being touched (grep across the codebase); a guard placed in
   the shared function is usually a *smaller* diff than patching each
   caller, and it's the only version that doesn't leave sibling callers
   still broken.
10. **Mark any deliberate corner cut.** A simplification with a known
    ceiling (a single global lock, an O(n²) scan, a naive heuristic) gets a
    short comment naming the ceiling and what would justify revisiting it
    — for example, `# simplification: global lock, move to per-key locks
    if throughput becomes an issue`.

## Pitfalls

- Writing a new helper before actually searching for whether one already
  exists nearby — the ladder only works if step 3 (search first) really
  happens.
- Adding a new dependency for something the standard library, a native
  platform feature, or an already-installed package already covers.
- Optimizing for the shortest diff in isolation from understanding the
  problem — the smallest change in the wrong place is a second bug, not a
  simpler one.
- Patching only the exact call site a bug report names, leaving every
  other caller of the same shared function still broken.
- Simplifying away input validation at a trust boundary, error handling
  that prevents data loss, or anything the user explicitly asked for —
  none of those are ever the target of a simplicity pass.
- Skipping rung 1 ("does this need to exist") and jumping straight to
  implementation, letting unrequested scope ship unnoticed.

## Verification

- For any new code added, there's a stated reason the earlier rungs (skip
  it, reuse from the codebase, standard library, native feature, existing
  dependency) didn't already cover the need.
- A bug fix addresses every caller of the affected shared function, not
  only the one path the original report named.
- No unrequested abstraction (an interface with a single implementation, a
  config knob for a value that never changes) was introduced.
- Any deliberate corner cut carries a comment stating its limit and what
  would trigger revisiting it.
