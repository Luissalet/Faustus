---
name: writing-plans
description: Turn a confirmed spec into a bite-sized implementation plan that names exact files, signatures, and tests per task, leaving nothing for the implementer to invent. Use when a spec or clear requirements already exist for a change spanning more than one obvious file, before writing any code.
version: 1.0.0
category: planning
tags: [planning, specs, task-breakdown, third-party]
status: published
source: imported
---

## When to Use

A multi-step change with a spec or clear requirements already in hand —
before touching code. Skip it for a single-line fix, a typo correction, or
a change whose correct implementation is already unambiguous; the plan
earns its cost on work an implementer (human or model) hasn't seen before
and can't safely improvise. Pair with `spec-driven-development` when no
spec exists yet. Adapted from https://github.com/obra/superpowers (MIT,
© 2025 Jesse Vincent).

## Procedure

1. **Check scope first.** If the spec actually bundles several independent
   subsystems, split into one plan per subsystem before writing task
   detail — each plan should produce working, testable software on its
   own. A plan covering unrelated subsystems is unreviewable as a whole.
2. **Map file structure before tasks.** Decide which files get created or
   modified and what each one is responsible for. Prefer several small,
   focused files over one that does too much; follow the codebase's
   existing conventions rather than unilaterally restructuring it.
3. **Open with a fixed header:** one-sentence goal, 2–3 sentence approach,
   the tech stack involved, and the spec's own project-wide constraints
   (version floors, naming rules, platform requirements) copied verbatim —
   every task implicitly inherits these. Include a short "review focus"
   list: the handful of failure modes the spec implies that no task's
   tests yet cover, ordered by how likely they are to actually bite a
   user.
4. **Size tasks so each carries its own test cycle.** A task is the
   smallest unit worth a fresh reviewer's gate: fold setup, config, and
   docs into the task whose deliverable needs them, and split only where a
   reviewer could reasonably approve one task while rejecting its
   neighbor. Every task lists its exact files (create/modify, with line
   ranges where useful, plus the test file) and an "interfaces" block
   naming exactly what it consumes from earlier tasks and produces for
   later ones — the implementer of one task should never have to read
   another task's code to learn a name or a type.
5. **Write steps at one-action granularity**, each with a checkable
   result: write the failing test (as actual code, with the spec's exact
   values), run it and confirm it fails for the right reason, implement
   the exact signature the task names (leave the body to the implementer
   unless an algorithm is genuinely non-obvious), run it and confirm it
   passes, commit.
6. **Keep every step unambiguous, not exhaustive.** A step is done when it
   lets the implementer write exactly one reasonable thing from it. A test
   step needs the test's name and assertions; a code step needs the exact
   signature and any spec-pinned values, not a full body the signature and
   tests already determine. A line that decides nothing ("handle edge
   cases", "add appropriate validation", "TBD") is a gap in the plan, not
   a shortcut.
7. **Self-review before handing it off:** walk the spec section by section
   and confirm a task implements each one; scan every step for ambiguity
   or an over-written body; check that types and signatures used in later
   tasks match what earlier tasks actually defined; confirm each
   review-focus item has a test in the task that owns it; and compare the
   plan's length to the spec's — a plan several times longer than its spec
   is a transcript of the code, not a plan.
8. **Get explicit confirmation before implementation starts.** Save the
   plan somewhere both the user and the implementer can reference it, ask
   whether it captures what they want, and only then begin executing task
   by task, re-running the narrow test for each task as you go (see
   `tdd-workflow` and `verification-loop`).

## Pitfalls

- Padding a plan with full function bodies "for clarity" — that writes the
  code twice, drifts from the real implementation immediately, and hides
  the actual decisions in a wall of code.
- Leaving a real decision unmade ("add proper error handling") instead of
  resolving it in the plan — that decision then gets made silently and
  inconsistently by whoever implements each task.
- Skipping the scope check on a request that actually bundles several
  independently-shippable capabilities, producing one oversized plan
  nobody can review in one sitting.
- Drawing task boundaries by "what looks like a natural chapter" instead of
  by what a reviewer could independently approve or reject.
- Never running the self-review pass, so a spec requirement with no task,
  or a function name that drifts between two tasks (`clearLayers()` in one,
  `clearFullLayers()` in another), ships unnoticed.
- Starting implementation before the user confirms the plan matches what
  they actually want.

## Verification

- Every requirement in the spec traces to a specific task in the plan —
  checked explicitly, not skimmed.
- Every step names an exact file, signature, value, or command; none
  require the implementer to guess or invent an interface.
- Type and signature names are consistent everywhere they're used across
  tasks.
- The plan is saved somewhere retrievable, and the user confirmed it
  before any task's implementation began.
