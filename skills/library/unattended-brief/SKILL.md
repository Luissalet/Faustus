---
name: unattended-brief
description: Turn a short ask into a brief that can run without the user watching — their words verbatim, an observable done-when, the feedback loop, constraints and neighbours, a budget, an absence window, stop conditions and a report contract — and close it with a done-audit that checks every criterion against evidence at the right level (claimed, pushed, green, merged, deployed, measured). Use when the user hands work off ("mientras duermo", "while I'm away", "kick this off", "hazlo esta noche"), before a night shift or a dispatch, or when they keep asking "¿cómo va?".
version: 1.0.0
category: planning
tags: [brief, handoff, unattended, night-shift, done-when, stop-conditions, progress, evidence, done-audit, encargo, mientras duermo]
status: published
source: imported
---

## When to Use

Twenty words are enough for a person who will answer questions; they are not enough for work that runs while nobody answers. A brief writes down what the run will need: what done looks like, how progress is measured, what must not be touched, how much it may spend, when to stop and when to tell. Every "¿cómo va?" the user types is a poll they run by hand — the report contract replaces it. Not for a short task the user is watching live.

Adapted from github/lever (MIT), skills brief and progress-contract, mapped onto Faustus's night shift, dispatch, objectives and criteria_check. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Ask verbatim.** Copy the user's words exactly at the top; never paraphrase the ask away.
2. **Done when:** observable, checkable by the agent itself (a test passes, a page shows X, a number crosses Y). If there is something to climb, attach or create a Loop Card (`measured-improvement`).
3. **Constraints and neighbours:** what must not change, other chats or agents working on the same area, each constraint with its source and expiry.
4. **Budget:** timebox, iterations, tokens or GPU time; stop cleanly when spent (night shift budgets do this).
5. **Absence window and authority:** when the user is away, what the run may do alone (commit, deploy, message), and the one question it may escalate.
6. **Stop conditions:** success, give-up (what counts as stuck: two hours without progress → re-plan with a simpler plan and a second opinion), escalate.
7. **Report contract:** milestones with ETAs and the evidence level each needs; report only when something changes; lead with the headline metric; at most one question, with a recommended answer.
8. **Run it** as a night shift, a dispatch job or the current chat's objective; set the criteria in `criteria_check` so the end can be audited.
9. **Done-audit before saying done:** re-read the verbatim ask and the done-when; for each criterion cite evidence at the required level (never round up: claimed < pushed < green < merged < deployed < measured), give a confidence, and mark anything without evidence "not done"; get an independent check (`goldfish_review` or a fresh worker) for anything under 80 %.

## Pitfalls

- A done-when only a human can judge ("make it nicer").
- No stop condition: the run either quits at the first obstacle or burns the budget.
- Status messages that replace delivery, or reports when nothing changed.
- Reporting "done" at the claimed level.
- Asking the same question every hour.

## Verification

- The brief has every section filled; empty ones are marked as decided-not-needed.
- The final report has an audit table criterion → evidence → level → confidence.
- The user did not need to ask for status during the run.
