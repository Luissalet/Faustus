---
name: coding-standards
description: The cross-language defaults for readable, simple, non-duplicated code — naming, immutability, function size, and when a project's own conventions override all of it. Use when writing or reviewing code in any language and no project-specific style guide says otherwise.
version: 1.0.0
category: engineering
tags: [coding-standards, readability, quality]
status: published
source: imported
---

## When to Use

Every time you write code, as the default that applies until the project's
own conventions (an existing style, a linter config, a documented rule)
override it. When the project disagrees with this skill, the project wins —
consistency with the surrounding code beats an abstract ideal.

## Procedure

1. **Readability first.** Optimise for the next reader, not for showing off
   a language feature. A clever one-liner that takes thirty seconds to
   parse is worse than three obvious lines.
2. **KISS.** Solve the problem in front of you, not the general version of
   it you can imagine. The general version, if it's ever needed, is easier
   to build once the specific one exists and is understood.
3. **DRY, but not reflexively.** Extract a shared helper once the same logic
   has appeared three times with the same meaning — extracting after the
   second occurrence often guesses wrong about what's actually shared.
   Two pieces of code that look similar but change for different reasons
   should stay separate.
4. **YAGNI.** Don't build the configuration option, the plugin hook, or the
   extra parameter for a use case nobody has asked for yet. It's cheaper to
   add later, with a real requirement in hand, than to maintain speculatively.
5. **Name for what it means, not what it is.** `elapsed_ms` beats `x`;
   `is_expired` beats a bare boolean called `flag`. A name should let a
   reader skip reading the implementation.
6. **Keep functions to one level of abstraction.** A function that mixes
   "loop over records" with "format a currency string" with "open a socket"
   is doing three jobs; each deserves its own name.
7. **Prefer immutable data where the language makes it easy** (returning a
   new value over mutating a shared one) — it removes a whole category of
   "who changed this and when" bugs, at the cost of an allocation that is
   almost never the actual bottleneck.
8. **Handle errors at the right level.** Catch what you can meaningfully
   recover from or add context to; let everything else propagate rather than
   swallowing it into a log line nobody reads.

## Pitfalls

- Applying a personal style preference over the project's existing,
  consistent one — "I would have named it differently" is not a reason to
  make the codebase inconsistent.
- Premature abstraction: a generic `BaseHandler` built for a second use
  case that never arrives, carried forever as unnecessary indirection.
- Deep nesting instead of early returns — a function four `if`s deep is
  harder to verify than the same logic with guard clauses at the top.
- Silent `except: pass` / empty `catch` blocks that hide a real failure
  as if it never happened.
- Comments that restate the code instead of explaining a non-obvious
  "why" — a comment that goes stale the moment the code changes is worse
  than no comment.

## Verification

- A reader unfamiliar with this specific change can follow it from names
  and structure alone, without running it mentally line by line.
- No function mixes unrelated levels of abstraction; each does one
  describable thing.
- Nothing was abstracted for a use case that doesn't exist yet in this
  codebase.
- The change matches the surrounding file's existing style, even where that
  style differs from this skill's defaults.
