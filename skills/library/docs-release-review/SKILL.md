---
name: docs-release-review
description: Bring a project's docs in line with its code before a release — a deterministic claim check first, then read-only reviewers per page and angle, then one editor per file, then a final verification against the code. Use when asked to update, audit or verify documentation for a release, or "are the docs still true".
version: 1.0.0
category: writing
tags: [docs, release, review, fan-out, verification]
status: published
source: imported
---

## When to Use

Docs that describe code: a manual, a README, API pages, examples, flags,
limits. Before a release, after a large refactor, or when someone asks
whether the documentation is still accurate. Not for prose-only writing
(marketing copy, a blog post) where there is no source to check against.

## Procedure

1. **Deterministic pass first, for free.** Run `doc_claims_check` on the doc
   files. Every broken reference (a path, symbol, setting, route or tool
   that no longer exists) and every stale section (its cited code changed
   after the section was last edited) goes on the work list without any
   model call. This is the part a reviewer most often skips.
2. **Plan the review as pages × angles.** For each page, one job per angle
   that applies: (a) claims vs the source, (b) what this release changed that
   the page does not say, (c) examples, flags and defaults still match the
   code (extract each code example and compile or run it when the language
   allows), (d) links and stated limits. Add site-wide jobs for
   cross-page consistency (two pages must not disagree about a timeout or a
   limit) and for anything known broken that the docs do not admit.
3. **Fan out read-only reviewers.** `delegate_agents` with `read_only: true`
   and `tier: "fast"` per job, one page and one angle each, `max_findings`
   4. Reviewers read and report; they never edit. Give each the page path,
   the angle and the source directories to check against. Wide is fine on
   a hosted fast tier; on one local GPU keep it to what the queue can drain.
4. **Merge findings into one list per file.** Drop duplicates, keep the
   file and line each finding rests on, and drop any finding the reviewer
   could not tie to the source.
5. **One editor per file.** `delegate_agents` with `files: [that page]`
   (ownership, so no two editors touch the same file) and `tier: "mid"`,
   instruction = that file's findings only. A page that says a feature is
   broken, when the code says so too, stays saying it — accuracy over
   comfort.
6. **Final verification.** One or two workers on `tier: "frontier"` check the
   edited pages against the code (not against the reviewers' notes), run
   `doc_claims_check` again, and compile or run the extracted examples.
   Then `git diff --check`.
7. **Report**: pages changed, what was wrong on each (a sentence), what was
   verified and how, and anything left that needs the owner.

## Pitfalls

- Letting reviewers edit: two workers rewriting one page collide, and a
  reviewer that edits stops reporting.
- Dumping the whole repo into every reviewer's context: one page, one angle,
  the source directories it needs.
- Trusting a reviewer's claim without a file and line: it goes back or out.
- Softening a documented limitation because it reads badly in a release.
- Skipping step 1: the deterministic check finds dead references for free
  that a model can miss.

## Verification

`doc_claims_check` reports no broken references on the edited pages, every
extracted example compiles or runs, `git diff --check` is clean, and each
change in the diff maps to a finding tied to the source.
