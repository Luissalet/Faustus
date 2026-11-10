---
name: cross-model-review
description: Review a diff, a plan, a spec or a skill with two or three different models working blind and independently, and keep only findings they agree on or that survive a fresh attempt to disprove them, at most five, silence being a valid result. Runs as goldfish_review flow=panel. Use when asked for a second opinion, "revísalo con otros modelos", "only show me what the models agree on", before a plan drives a day or more of work, or when work has been stuck for hours and a simpler plan needs a critique.
version: 1.0.0
category: engineering
tags: [review, panel, multi-model, second-opinion, blind-review, agreement, proof-table, plan-critique, goldfish_review, segunda opinión, revisión]
status: published
source: imported
---

## When to Use

Different models make different mistakes. Reviewed blind and independently, their agreement is evidence; a finding only one model raised has to earn its place. Use it for a pull request or diff before handing it over, for a plan before spending a day on it, for a skill or prompt before relying on it, and to rubber-duck a simpler plan when work stalled. Not for style (a linter's job), not as the only gate before merging (tests still decide), not on drafts nobody asked to review.

Adapted from github/lever (MIT), skill cross-model-review, implemented by `goldfish_review` flow `panel`. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Build one packet for everyone:** the diff or plan as a file (or text), plus the files a reviewer needs to judge it (`context_files`: touched files, the issue, the brief). Leave out the author's opinion, other reviews and any hint of the expected answer — that is what blind means.
2. **Pick 2–3 different models** (`models`; without it the panel takes the enabled local models). Different families beat two sizes of one family.
3. **Run** `goldfish_review` with `flow: panel` and `panel_mode: defects` (wrong behaviour, data loss, security, broken contracts; each finding with location, mechanism, a failing case, P1–P3 and confidence) or `panel_mode: plan` (wrong assumptions, what cannot be measured, unnecessary constraints, the simplest plan).
4. **Let the bucketing work.** Findings are matched by location and mechanism; two or more models = agreed; a single or contradicted finding goes to a fresh worker that fills a proof table (premise, counter-evidence searched, before/after, reachability, consequence, remedy, confidence) and survives only with high confidence. When models disagree on severity the lower one counts.
5. **Read the outcome:** ACTED with at most `max_findings` (default 5), agreed first, or NOOP. Open `panel_report.md` and `findings.json` with `action: status, artifact: …` to see what was dropped and why.
6. **Act.** Fix agreed findings or answer each with a reason; for a plan, revise it and write down what changed. The author decides; reviewers advise.

## Pitfalls

- Pasting your own reading of the change into the packet: the panel stops being blind.
- Two models from the same family agreeing on the same blind spot; mix families.
- Treating a single-model finding as fact because it sounds plausible.
- Reviewing every small push: cost grows with triggers, not with value.
- Ignoring NOOP: silence on a clean change is the expected result.

## Verification

- The report lists which models ran and that each completed.
- Every reported finding is either agreed or carries a proof table that survived.
- Each finding has an outcome: fixed, answered with a reason, or handed to the user.
