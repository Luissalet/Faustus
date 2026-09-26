---
name: code-review-checklist
description: Review a change across five fixed axes — correctness, readability, architecture, security, and performance — labeling every finding by severity before calling it mergeable. Use when about to merge any pull request or accept AI-generated or another agent's code, especially where no formal review process already covers it.
version: 1.0.0
category: engineering
tags: [code-review, quality, checklist, third-party]
status: published
source: imported
---

## When to Use

Before merging any non-trivial change — a new feature, a bug fix (review
both the fix and its regression test), a refactor, or code produced by
another agent or model. Treat AI-generated code as needing *more*
scrutiny, not less: it is confident and plausible even when wrong.
Complements `verification-loop` (which checks the change runs and passes)
by checking whether the change is *good*, not just working. Adapted from
https://github.com/addyosmani/agent-skills (MIT, © 2025 Addy Osmani).

## Procedure

1. **Understand intent before reading the diff.** What is this change
   trying to accomplish, what task or spec does it implement, what
   behavior should differ afterward.
2. **Read the tests first.** Do they exist, do they test behavior rather
   than implementation detail, do they cover the edge cases a reasonable
   person would expect, would they actually catch a regression if the
   surrounding code changed?
3. **Walk the diff against five fixed axes, roughly in this order of
   leverage:**
   - **Correctness** — matches the spec/task; null, empty, and boundary
     cases handled; error paths handled, not just the happy path; no
     off-by-one or state-consistency bug.
   - **Readability** — names are descriptive and consistent; control flow
     avoids nested ternaries and deep callbacks; no dead code, no-op
     variables, or leftover "removed" comments.
   - **Architecture** — fits existing patterns or justifies a new one; no
     duplicated logic that should be shared; dependencies flow one
     direction; a "cleanup" that relocates complexity instead of removing
     it is not an improvement — count the concepts a reader must hold
     before and after.
   - **Security** — input validated at trust boundaries; no secrets in
     code, logs, or version control; queries parameterized, not
     string-built; external data (APIs, config files, user content)
     treated as untrusted until validated.
   - **Performance** — no N+1 query pattern; no unbounded loop or fetch;
     list endpoints paginated; no large object built in a hot path.
4. **Label every finding with a severity** so the author knows what's
   required versus optional: **Critical** blocks merge (security hole,
   data loss, broken functionality); no prefix means required before
   merge; **Nit** is optional/style, safe to ignore; **Consider** /
   **Optional** is a suggestion.
5. **Lead with what matters.** Order findings correctness and security
   first, then structural issues, then everything else — one real
   structural problem outweighs ten cosmetic nits and should never be
   buried under them.
6. **Check the verification story.** What was actually run — which tests,
   did the build pass, was it exercised manually — not "should work."
   Ask for it explicitly if it's missing from the change description.
7. **Flag dead code created by the change** explicitly (a function
   replaced but not deleted, a constant no longer referenced) and ask
   before removing anything not certain to be unused.
8. **Approve when the change measurably improves the codebase** and
   follows its conventions, even if it isn't exactly how you'd have
   written it — don't block on a personal style preference the project's
   own conventions are silent on.

## Pitfalls

- "LGTM" with no evidence any of the five axes were actually checked.
- Checking only that tests pass while skipping architecture, security, and
  performance entirely.
- Giving every comment the same weight, so the author can't tell a
  blocking issue from a nitpick without guessing.
- Softening a real bug into "might be a minor concern" instead of stating
  it plainly and quantifying its impact when possible.
- Accepting "I'll clean it up later" for something addressable now —
  deferred cleanup reliably never happens; require it before merge or file
  a tracked follow-up.
- Reviewing a change too large to hold in working memory at once instead
  of asking the author to split it by file group, layer, or feature slice.
- Treating a version/dependency bump as low-risk because it's "just a
  bump" — read the changelog, and prefer one dependency per change so a
  break is attributable.

## Verification

- Every one of the five axes was explicitly considered, not only
  correctness.
- Every finding carries a severity label, and every Critical or required
  finding is resolved — or explicitly deferred with a stated reason —
  before approval.
- The author's verification story (what was run, what passed) is recorded
  in the review, not assumed.
- The verdict states approve or request-changes explicitly, never left
  implicit.
