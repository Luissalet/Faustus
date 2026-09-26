---
name: spec-driven-development
description: Write a short spec — objective, commands, structure, testing strategy, and explicit always/ask-first/never boundaries — before starting new or ambiguous work, surfacing every assumption for the user to confirm instead of silently filling gaps. Use when starting a new feature with no existing specification, or when requirements are vague, incomplete, or only exist as an idea.
version: 1.0.0
category: planning
tags: [planning, specs, requirements, third-party]
status: published
source: imported
---

## When to Use

Starting a new project or feature, or facing requirements that are
ambiguous, incomplete, or bundle several distinct capabilities. Skip it for
a single-line fix or a change whose correct implementation is already
unambiguous — a two-line spec is fine when that's genuinely all there is to
say. Feeds directly into `writing-plans` once confirmed, and into
`pretask-interview` when the open questions need a back-and-forth rather
than a one-shot list. Adapted from https://github.com/addyosmani/agent-skills
(MIT, © 2025 Addy Osmani).

## Procedure

1. **Check whether this is really several capabilities.** If the request
   names distinct concerns with their own consumers or data (identity,
   billing, notifications), and its acceptance criteria cluster into
   groups that could ship separately, sketch a short capability map first
   — module id, one-line responsibility, and what it depends on — and get
   it confirmed before writing any module's spec. Getting the boundaries
   wrong later is expensive; reviewing a five-row table now is not. Most
   requests describe one capability; this step exists for the exception.
2. **List every assumption before writing the spec.** State plainly what
   you're about to bake in ("assuming this is a web app, not native
   mobile; assuming session-based auth; assuming Postgres based on the
   existing schema — correct me now or I'll proceed with these"). The
   entire value of a spec is catching a misunderstanding before code
   exists; an assumption made silently defeats that purpose.
3. **Write a short spec covering:**
   - *Objective* — what's being built, why, who the user is, what success
     looks like.
   - *Commands* — the actual build/test/lint/dev commands, not just tool
     names.
   - *Structure* — where source, tests, and docs live.
   - *Style* — one real code snippet showing the convention to follow beats
     three paragraphs describing it.
   - *Testing strategy* — framework, test locations, what needs coverage
     and at what level.
   - *Boundaries* — three tiers: **always do** (run tests before commit,
     validate inputs), **ask first** (schema changes, new dependencies, CI
     changes), **never do** (commit secrets, edit vendored code, delete
     failing tests without approval).
4. **Reframe vague requirements as testable criteria.** "Make the dashboard
   faster" becomes specific numbers (load time, response time) the user
   confirms or corrects — a target you can check beats a word you can't.
5. **Get the spec reviewed and explicitly confirmed** by the user before
   producing an implementation plan from it (see `writing-plans`).
6. **Keep the spec alive.** When a decision changes mid-implementation,
   update the spec first, then the code — an outdated spec left unedited is
   worse than an honestly incomplete one, because it actively misleads the
   next reader.

## Pitfalls

- Writing code first and the spec afterward as documentation — that
  captures what was built, not what should have been decided beforehand,
  and never gets the chance to catch a misunderstanding early.
- Silently resolving an ambiguous point instead of listing it as an
  assumption for the user to confirm or correct.
- Treating "requirements will change anyway" as a reason to skip the spec
  rather than a reason to keep updating it as they do.
- Writing one oversized spec for a request that actually bundles several
  independently-shippable capabilities, forcing every downstream task to
  reason about the whole bundle at once instead of its own module.
- Leaving boundaries implicit ("obviously don't touch prod config")
  instead of writing them into always/ask-first/never — the boundary that
  was never written down is the one that gets crossed.

## Verification

- The spec states objective, commands, structure, testing strategy, and
  explicit boundaries, and is saved somewhere both the user and later work
  can reference.
- Every assumption made while drafting it was listed and either confirmed
  or corrected by the user.
- Success criteria are specific enough that someone else could check them
  without asking what the words mean.
- If the request bundled multiple capabilities, a confirmed capability map
  exists and each module's spec traces to one entry in it.
