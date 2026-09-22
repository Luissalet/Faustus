---
name: council
description: Weigh a genuinely hard decision from several independent perspectives in parallel, then synthesize a verdict that names the trade-off instead of hiding it. Use when a decision is contested, expensive to reverse, or has plausible experts who would disagree (architecture choices, a risky migration, a build-vs-buy call).
version: 1.0.0
category: planning
tags: [decision-making, deliberation, architecture]
status: published
source: imported
---

## When to Use

A decision that is genuinely hard to reverse and where reasonable people
with different priorities (performance vs. simplicity, speed vs.
correctness, short-term vs. long-term cost) would land differently. Not for
routine choices with an obvious right answer — a council for "should this
function be async" is theatre, not deliberation.

## When NOT to Use

Anything reversible cheaply, anything with a clear best practice already
established in this codebase, or anything small enough that the cost of
deliberating exceeds the cost of just picking one and adjusting later.

## Procedure

1. **Extract the real question.** Restate the decision in one sentence, and
   separately name what's actually at stake if it goes wrong — money, time,
   a painful migration later, user-visible breakage.
2. **Gather only the necessary context** — the constraints that actually
   bind (existing infrastructure, team size, deadline, data volume), not
   an exhaustive survey. A council drowning in context deliberates on
   trivia.
3. **Form an initial position** from the most structurally-grounded
   perspective available (an architecture-minded read of the actual
   constraints) as an anchor — not the final answer, just a starting point
   the other perspectives can agree or disagree with explicitly.
4. **Launch a small number of genuinely independent perspectives in
   parallel** — using `delegate_agents` to run each one as its own
   worker with its own framing (e.g. one optimising for long-term
   maintainability, one for the fastest safe path to shipping, one for
   risk/failure modes) so they don't anchor on each other's wording before
   forming a view.
5. **Synthesize, not average.** Read all positions, note where they agree
   (usually the load-bearing facts), where they genuinely conflict (usually
   the actual trade-off), and don't force a fake consensus — a synthesis
   that says "some said A, others said B, and here's why" is more useful
   than a mushy middle nobody actually argued for.
6. **Present a compact verdict**: the decision, the trade-off it accepts,
   and what would change the answer (a fact that, if different, flips the
   recommendation) — so the reader can sanity-check it against their own
   knowledge of what's actually true.
7. **Persist the verdict** where the decision is recorded (an architecture
   note, a decision log) so the reasoning is auditable later, not just the
   conclusion.

## Pitfalls

- Running every "perspective" from the same framing with different labels —
  if none of them would actually disagree, it isn't a council, it's one
  opinion repeated three times.
- Padding the context each perspective receives until they all converge on
  the obvious answer instead of surfacing the real tension.
- Averaging conflicting recommendations into a compromise nobody actually
  argued for, rather than naming the trade-off and picking a side.
- Using this for a decision that was never actually contested — the
  overhead only pays off when the disagreement is real.

## Verification

- The verdict names the specific trade-off accepted, not just the choice.
- At least one perspective's position actually differs from the others in a
  way that mattered to the outcome — if they were identical, re-check the
  framing next time.
- The reasoning, not just the conclusion, is recorded somewhere retrievable.
