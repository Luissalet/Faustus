---
name: seed-coach
description: Rehearse an investor call - drill the hard questions investors ask (market, competition, traction, unit economics, team, why now, risks, the ask) tailored to the founder's own numbers, competitors and the specific investor, grade written answers against five checks, and write a 20-minute call agenda; with People's Hoard for the investor's history when connected. Use when someone asks to prepare for an investor call, practise a pitch, know what investors will ask, be drilled, have an answer graded, or after an investor call is booked.
version: 1.0.0
category: planning
tags: [startup, fundraising, seed, pitch, coaching, investor-call, rehearsal, questions, agenda, people, preparar-reunion, ensayo, third-party]
status: published
source: imported
---

## When to Use

Before a first call with an investor, or any time the founder wants to practise. It reads the numbers the other steps wrote, turns them into the questions a sharp investor will ask about them with the founder's figures beside each, grades answers, and lays out the 20 minutes so they go on the story, not on small talk. It never invents a number for an answer, never tells the founder to overstate traction and never implies a good call means a check.

Eighth of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-closer` books a call; next is `seed-terms` or `seed-dataroom`. The full question bank is in `references/question-bank.md`.

## Procedure

1. Read what exists in `seed/` (working files live in a `seed/` folder of the project workspace, or as Cerebro notes with `brain`): `competitors.md`, `model.md`, `market.md`, `plan.md`, `investors.md`, `pipeline.md`. For a named investor, also read their row and, when People's Hoard is connected, `find_people` then `prepare_person_chat` (facts, recent interactions, open commitments both ways).
2. Pick ten questions from `references/question-bank.md`, tailored ones first. A tailored question is asked only when every number it needs is in the files; otherwise skip it, never fill it with a guess. Ask `ask_user` for the words the founder uses for customers and the supply side so the questions sound like their business.
3. Ask one question at a time, as the investor would, with no hint first. Let the founder answer in writing or out loud (transcribed).
4. Grade each answer with five checks of 20 points: (a) 15 to 150 words, so under a minute spoken at about 150 words a minute; (b) contains a number; (c) no hedging words ("I think", "maybe", "probably"); (d) at least two specifics (numbers, names) and no buzzwords ("disrupt", "world-class"); (e) the answer first: a first sentence of 25 words or fewer. Count words and numbers by reading, or with `python` for exactness. Read the unchecked lines back and ask for a tighter version; two or three rounds per question.
5. When a numbers question stumps the founder, show "your numbers" and the file they came from. If a number looks wrong, fix it in its own step (`seed-model`, `seed-market`), not in the answer.
6. Write the agenda for a booked call as seven blocks, 2, 3, 4, 4, 4, 2 and 1 minutes for 20 (scale exactly for other lengths): hello and why them (open with their personal line), the problem (the review evidence), the product, market and model, their questions (the three most likely), the ask, the next step. Save to `seed/coach/agenda-<investor>.md` and walk through it the day before. With `manage_calendar` offer a prep block the day before; create it only if the user agrees.
7. After the call, hand back: log it (People's `log_interaction`, and its commitments with `commitments_list` or `commitment_add` when connected) and send the follow-up through `seed-closer`; `seed-dataroom` if they ask for documents.

Next: `seed-terms` when an investor sends paper, `seed-dataroom` when they ask for documents.

## Pitfalls

- Supplying a strong-sounding number the founder does not have. A good answer says "we do not know yet, and this is how we will find out".
- Drilling a tailored question on stale files: check the plan is not older than the model.
- Grading by feel. State which of the five checks failed.
- Coaching toward hype, hedging dressed as modesty, or a long preamble before the answer.
- Reusing the same ten questions every round; vary categories and investors.

## Verification

- Each asked tailored question had all its numbers in a file, shown to the founder.
- Every graded answer lists the five checks with pass or fail and the total.
- The agenda's blocks add up to the call length and reference the founder's real evidence.
- No number was suggested that is absent from the files.
