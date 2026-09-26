---
name: pretask-interview
description: Interview the user in batched rounds shaped like a decision tree before starting ambiguous or expensive-to-redo work, asking only what genuinely needs a human choice and recommending an answer for everything else. Use when requirements for a plan, design, or architecture decision are vague, contested, or costly to get wrong.
version: 1.0.0
category: planning
tags: [planning, requirements, interview, third-party]
status: published
source: imported
---

## When to Use

Before committing to a plan, design, or architectural decision that is
genuinely ambiguous or expensive to redo — not for routine tasks with one
obvious approach. Skip it when the request is already fully specified;
running an interview over a settled question wastes the user's attention
and delays work that could just start. Pairs well with
`spec-driven-development` as the step that resolves what the spec leaves
open. Adapted from https://github.com/mattpocock/skills (MIT, © 2026 Matt
Pocock).

## Procedure

1. **Map the decision as a tree.** Write down the top-level choice and
   every sub-decision that only makes sense once something above it is
   settled (e.g. "which auth provider" only matters after "do we need
   multi-tenant auth at all" is answered).
2. **Compute the frontier.** The frontier is every question whose
   prerequisites are already settled — the ones answerable right now
   without guessing at something not yet decided. A question that depends
   on another still-open question belongs to a *later* round, not this
   one.
3. **Ask the whole frontier together, in one round**, never one question
   at a time — a decision interview run question-by-question multiplies
   round-trips for no benefit. Number each question, state it concretely
   (including the small fixed set of options when one exists), and give
   your own recommended answer for every question — never hand the user a
   blank question with no default to react to.
4. **Never ask what you can find yourself.** Before adding a frontier
   question that turns on the codebase, existing config, or stored data,
   look it up (read the file, grep for the pattern, run the query) and
   only put genuinely human decisions — trade-offs, priorities, product
   calls — in front of the user. A running lookup that hasn't finished
   yet is itself an unsettled prerequisite: don't block the rest of the
   frontier on it, just hold back the questions that depend on its answer.
5. **Wait for answers before computing the next round.** Each answer can
   unblock new frontier questions, or make a previously-planned question
   moot — recompute the tree rather than asking a pre-written list.
6. **Repeat rounds until the frontier is empty** — every branch of the
   tree visited, nothing left silently assumed. A decision the user never
   explicitly made is not settled just because nobody objected.
7. **Summarize the settled decisions in one place** and get explicit
   confirmation that this is the shared understanding before acting on any
   of it.

## Pitfalls

- Asking one question, waiting for the reply, then asking the next — batch
  the entire current frontier every round instead.
- Asking the user something a file read or a command could have answered —
  this wastes their attention and signals the environment wasn't checked
  first.
- Silently filling in an assumption because "it seemed obvious," instead of
  surfacing it as a question — obvious to the person who's been staring at
  the code is rarely obvious to the person who has to live with the
  decision.
- Declaring the interview done after one round when a later answer reopens
  a branch that depended on it.
- Giving no recommended answer and making the user do all the thinking on
  every question — a sensible default speeds up every round and gives the
  user something concrete to correct.

## Verification

- Every leaf of the decision tree was either explicitly asked and
  answered, or resolved by something looked up directly — none were
  silently assumed.
- No question was put to the user that a file read, grep, or command could
  have answered instead.
- The user explicitly confirmed the final summary of decisions before any
  implementation began.
- If an early round's answer reopened a later branch, that branch was
  revisited rather than left on its original assumption.
