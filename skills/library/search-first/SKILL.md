---
name: search-first
description: Check whether a well-known library or documented approach already solves the problem before writing custom code for it. Use when about to implement something that smells like a solved problem — parsing a common format, talking to a well-known API, a standard algorithm — especially in an unfamiliar domain.
version: 1.0.0
category: research
tags: [research, search, tooling]
status: published
source: imported
---

## When to Use

Before writing non-trivial custom code for something that plausibly has a
mature existing solution: a file format parser, an integration with a
well-known external API, a standard algorithm, a common infrastructure
pattern. Skip it for something genuinely specific to this codebase's own
domain logic — there's nothing to search for.

## Procedure

1. **State what you'd otherwise build**, in one sentence, before searching —
   this keeps the search focused and gives you something concrete to
   compare candidates against.
2. **Search for the problem, not a guessed solution name.** "Parse iCal
   recurrence rules python" finds more than guessing a library name that
   might not exist, using `web_search`.
3. **Read past the first result.** Check the library/approach's actual
   maturity: last-updated date, how it's licensed, whether it fits the
   project's existing dependency footprint — using `web_fetch` on the
   project's own docs/README rather than trusting a summary.
4. **Compare against "just write it"** on the axes that matter here: lines
   of code saved, edge cases it already handles that you'd otherwise
   discover the hard way, and the cost of a new dependency (maintenance,
   supply-chain surface, bundle/install size).
5. **Decide and say why**, briefly — "used library X because it already
   handles timezone edge cases we'd otherwise get wrong" is a decision a
   reviewer can evaluate; silently picking one is not.

## Decision Matrix

- **Small, stable need, no good match found** → write it yourself; a
  20-line function beats a dependency for something this narrow.
- **Well-known problem, well-maintained match exists** → use it; reinventing
  a mature library's edge-case handling is a false economy.
- **Large need, found but poorly maintained** → weigh carefully: vendoring
  the useful part of an abandoned small library can beat depending on it
  live, and can beat rewriting it from scratch.
- **No clear match either way** → say so explicitly rather than silently
  defaulting to custom code because the search didn't turn up anything in
  thirty seconds.

## Pitfalls

- Committing to the first search result without checking it's still
  maintained or actually fits the license/dependency constraints of this
  project.
- Skipping the search because "this feels like something I could just
  write" — that feeling is often wrong for anything involving dates,
  timezones, encodings, or a well-known external API's quirks.
- Searching once and stopping at "nothing came up" for a query that was
  itself too narrow or used the wrong terminology — try the problem
  statement, not just the first guess at a name.
- Adding a heavy dependency for one small feature when the project already
  has a lighter tool that does 90% of the job.

## Verification

- The decision (build vs. use an existing solution) is stated with a
  concrete reason, not left implicit.
- If an existing library was chosen, its maintenance status and license
  were actually checked, not assumed.
- If custom code was chosen over an existing option, the reason names what
  the existing option didn't fit (license, weight, missing feature) rather
  than "didn't look hard enough".
