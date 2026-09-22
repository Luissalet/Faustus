---
name: intent-driven-development
description: Turn a vague request into an explicit goal, scope, and a short list of observable acceptance criteria before writing any code. Use when a request is ambiguous about what "done" means, touches a risky area, or is large enough that scope creep would go unnoticed.
version: 1.0.0
category: planning
tags: [planning, requirements, acceptance-criteria]
status: published
source: imported
---

## When to Use

A request whose done-state is unclear, a change to something risky
(billing, auth, data deletion), or anything big enough that "I'll know it
when I see it" isn't a safe way to scope it. Skip the ceremony for a request
that is already unambiguous and small.

## Procedure

1. **Establish the goal and the risk level first.** State the outcome in one
   sentence, in the user's terms, not implementation terms. Separately, name
   what's risky about it: irreversible actions, money, auth, data that can't
   be recovered if deleted wrong.
2. **Discover context before defining scope.** Read the relevant code,
   check for existing similar features, and note any constraint the user
   didn't mention but that clearly applies (an existing API contract, a
   compliance rule, a performance budget).
3. **Define scope explicitly — what's in, what's out.** A one-line "out of
   scope: X, Y" prevents both scope creep during implementation and a
   disappointed reviewer expecting X and Y to be covered.
4. **Write acceptance criteria as observable behaviour**, each one testable
   independently: "AC-1: submitting the form with an empty email shows a
   validation error and does not create an account" — not "handle invalid
   input" or "the form should work correctly".
5. **Cover only the boundaries that are actually relevant** to this change —
   empty input, the maximum size, a concurrent second request, a network
   failure mid-operation — chosen because they're plausible here, not a
   generic checklist applied without judgment.
6. **Present the brief before implementing**, for anything with the risk
   flagged in step 1 — a short review of the acceptance criteria is far
   cheaper than discovering a misunderstanding after the change ships.
7. **Choose the right depth.** A quick capture (goal + 3-5 bullet criteria)
   is enough for a routine feature; a full brief with context and boundary
   coverage is worth it for something risky or ambiguous; reviewing an
   existing spec/ticket is enough when one already exists and is current.

## Pitfalls

- Writing acceptance criteria as restated requirements ("the feature should
  work") instead of observable behaviour someone could actually check against
  a running system.
- Skipping the "what's out of scope" line and then either overbuilding, or
  getting blamed for not covering something nobody agreed was in scope.
- Picking boundary cases from a generic checklist rather than the ones that
  are actually plausible for this specific change — testing for a 10 GB
  upload on a form that will only ever see a few kilobytes.
- Treating the brief as a one-time artifact instead of updating it when the
  understanding of the request changes mid-implementation.

## Verification

- Every acceptance criterion is phrased as something you could verify by
  observing behaviour, not by reading intent.
- The explicit out-of-scope list matches what actually didn't get built.
- For a risky change, the brief was shared and acknowledged before
  implementation started, not written retroactively to justify what was
  already built.
