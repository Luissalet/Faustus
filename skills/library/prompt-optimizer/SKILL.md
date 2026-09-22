---
name: prompt-optimizer
description: Diagnose a vague or underspecified prompt against a fixed checklist — intent, scope, missing context, tech stack — and rewrite it into something a model can execute without guessing. Use when a request is short, ambiguous, or missing information you'd otherwise have to assume before starting work.
version: 1.0.0
category: planning
tags: [prompting, clarity, planning]
status: published
source: imported
---

## When to Use

A request that is too short or ambiguous to act on safely: no clear success
condition, no mention of which files or stack, or a phrase that could mean
two very different things. Not needed when the request is already concrete
and scoped — rewriting a clear prompt just adds ceremony.

## Procedure

1. **Detect the project context first.** Check the actual repository
   (languages, frameworks, existing patterns) rather than the prompt's own
   wording — a request that says "add auth" means something very different
   in a FastAPI service than in a static site, and the codebase, not the
   phrasing, tells you which.
2. **Detect the intent type.** Is this a new feature, a bug fix, a refactor,
   a question, an exploration? Each implies a different depth of process
   (see `intent-driven-development` for the acceptance-criteria side of
   this) and a different set of skills worth pulling in.
3. **Assess scope.** Small (one file, low risk), medium (a few files, some
   design judgment), or large (architectural, cross-cutting, worth a written
   plan before touching code). Misjudging this in either direction is the
   single biggest source of wasted effort — over-planning a typo fix, or
   diving straight into code on something that needed a plan first.
4. **Match relevant skills/rules to what you found** — the tech stack and
   intent type usually point at specific per-language patterns or
   process skills (testing, security review) worth loading before starting,
   rather than proceeding on defaults.
5. **Detect missing context** the prompt didn't supply but the work needs:
   which environment, which existing endpoint/table it relates to, what the
   success condition actually is, whether backward compatibility matters.
6. **Recommend depth and, where relevant, delegation** — whether this is a
   quick single-pass task or one that benefits from a plan step and/or
   splitting across sub-agents (see `team-agent-orchestration`).

## Output Format

State the diagnosis briefly (intent, scope, what's missing), then the
optimised version of the request: concrete, scoped, naming the files or
areas involved and the success condition — plus, if anything was genuinely
ambiguous, the specific question that needs an answer before proceeding
rather than a guessed assumption buried in the rewrite.

## Pitfalls

- Rewriting a prompt into something the model wants to hear rather than
  what the request actually needs — padding a rewrite with plausible-
  sounding detail the user never stated is worse than asking.
- Guessing at missing context instead of naming it as an open question —
  a wrong silent assumption compounds through everything built on it.
- Treating every short request as under-specified; some short requests are
  short because they're genuinely simple.
- Over-scoping: turning a two-line fix into a multi-phase plan because the
  checklist has boxes to fill, not because the task needs it.

## Verification

- The rewritten request names a concrete success condition a reader could
  check against the finished work.
- Anything genuinely ambiguous is flagged as an explicit question, not
  quietly resolved by assumption.
- The assessed scope matches what the work turned out to need — if it
  didn't, that's worth noting for the next diagnosis.
