---
name: elephant-goldfish-design
description: Take an idea to an approved design document with the elephant-and-goldfish method run by Faustus's own workers — brainstorm through seven fresh lenses, draft section by section with the context-holding side, reconcile the draft against the brief, review it in five stages or in a loop where every finding is fixed, contested with evidence, escalated or declined until two rounds in a row are clean — all through goldfish_review. Use when a feature, a system change, a proposal or any document must stand on its own for readers who were not in the conversation, or the user asks to "redactar el diseño y que lo revisen peces".
version: 1.0.0
category: planning
tags: [design-doc, specification, brainstorm, draft, review-loop, goldfish, elephant, fresh-context, goldfish_review, diseño, especificación, revisión]
status: published
source: imported
---

## When to Use

Long conversations make the writer blind: decisions taken twenty turns ago feel written down when they are not. The elephant (the side holding the context) writes and judges; goldfish (workers that start empty) read only the page and its reading list and trip where a newcomer will. Use for design documents, specifications, implementation plans, proposals and READMEs that others will act on. For a quick review of an existing document only, `fresh-context-review` is enough.

Adapted from google/elephant-goldfish-model (Apache-2.0), skills egm-brainstorm, egm-draft, egm-review, egm-review-loop and egm-resolve, implemented by `goldfish_review`. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Brainstorm (optional).** `goldfish_review flow=brainstorm brief=<the idea>` with `context` (constraints, what exists) and `context_files` (code the idea touches). Seven lenses (YAGNI and reuse, ergonomics, architecture and cost, coupling, readiness, framing, false compromises) feed a concepts brief with goals, non-goals, alternatives weighed and the decisions left to the user. Settle those decisions with the user, one question at a time.
2. **Draft.** `flow=draft brief=<the concepts brief or notes> context=<decisions> save_to=<path>`. The elephant writes overview and problem, goals and non-goals, open questions, user journeys, technical plan, alternatives (each rejected with a concrete reason), and a detailed implementation plan (every file, create/modify/delete, phases, tests). Where the context does not settle a choice, it goes to Open Questions, not a guess.
3. **Reconcile.** The draft run compares the draft with the brief and writes `reconciliation.md`: contradictions first. Ask the user about each; the draft may be right because it read code the brainstorm did not.
4. **Review loop.** The draft flow continues into the loop (or start `flow=loop path=<doc>` on any document): new goldfish each round (`reviewers`, `standards`), the elephant classifies each finding — PROPOSE FIX with exact edits, CONTEST with evidence the reviewer could read, ESCALATE when the evidence lives only in the context, DECLINE for nits — and fixes. Converged = two clean rounds in a row; cap 5; churn (new findings where the last fixes landed) stops it. With `auto_apply: false` the run waits for your choice per finding in Studio (Goldfish reviews screen) or `action: resolve`.
5. **Full staged review** before approval: `flow=review preset=full` (zoom out with the inventive lens, comprehension with the reader's own paraphrase, critic against the feature-spec standard, zoom out again, implementation readiness against the zero-decision standard) → `consolidated_review_report.md` with a traceability matrix.
6. **Hand back** the final document (`final_file`), the open findings, every ESCALATE (they need the user's knowledge) and the reconciliation decisions.

## Pitfalls

- Putting the context in `context_files`: reviewers would see it. Decisions go in `context` (elephant only).
- Contesting a comprehension gap: if a fresh reader could not follow it, the document is missing something.
- Adding text only to satisfy a reviewer that a human reader would not need: escalate instead.
- Changing a finding's severity instead of contesting it.
- Stopping after one clean round: the next fresh reader may find a different gap.

## Verification

- Every round shows each reviewer as completed or not run (a not-run reviewer makes the round not clean).
- Every finding has an outcome: fixed (edit applied), contested with evidence, escalated to the user, or declined.
- The run ended converged, or the open findings at the cap are listed for the user.
- The document states the decisions the first round found missing.
