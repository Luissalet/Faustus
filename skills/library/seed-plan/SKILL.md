---
name: seed-plan
description: Turn a startup idea into the nine-section business plan investors expect (executive summary, problem, solution, market, competition, business model, go-to-market, financial plan, use of funds), pulling every number from the competitor, market and model files, working out the unit economics, and checking for empty sections and numbers that disagree between files. Use when someone asks to write or check a business plan, turn an idea into a plan, plan a startup, or wants unit economics, or before the pitch deck.
version: 1.0.0
category: planning
tags: [startup, fundraising, seed, business-plan, plan, unit-economics, executive-summary, startup-idea, plan-de-negocio, third-party]
status: published
source: imported
---

## When to Use

The founder wants the document behind a raise, written with them, with no number that is not in a file. It sets the startup's shared facts, lays out the nine sections, stitches in what the other steps found and checks the whole for holes: an empty section, a raise that is $1.5M in one file and $1.2M in another, a use of funds that does not add up. A plan is a way to think clearly, not a promise of funding.

Fourth of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-model`; next is `seed-deck`.

## Procedure

1. Write `seed/startup.md` (working files live in a `seed/` folder of the project workspace, or as Cerebro notes with `brain`), asking with `ask_user` for what is missing: name, one-liner, tagline, sector, stage, geography, the raise and the instrument (for example a post-money SAFE), and the founder's real name, the work email outreach will go from, and a postal address. Never put anyone else's email there, and never keep an example value the founder did not confirm.
2. Check that `seed/competitors.md`, `seed/market.md` and `seed/model.md` exist. If one is missing, run its step first (`seed-scout`, `seed-market`, `seed-model`); the plan's numbers come from those files, not from memory.
3. Write the nine sections into `seed/plan.md` with the founder, one at a time, in plain words and short paragraphs: executive summary, problem, solution, market, competition, business model, go-to-market, financial plan, use of funds. Every number must exist in one of the files; if the founder gives a new one, add it to the right file first (market inputs, model assumptions).
4. Give each section a one-line `pitch` (one slide's worth; the deck allows 35 words a slide), the solution and go-to-market up to three points each, a tagline, and the team: real people with real roles only.
5. Compute the unit economics from the model's assumptions and show them in the plan: revenue per order = price x take rate; contribution per order = that minus the per-order costs; CAC payback = CAC / (contribution per order x orders per customer a month); LTV = monthly contribution per customer / monthly churn; LTV:CAC = LTV / CAC (most seed investors look for 3x or more).
6. Run the checks. **Fix before anyone reads it:** an empty section; a missing market, model or competitor file; a raise that differs between files; a use of funds that does not add up to the raise; SOM above SAM or SAM above TAM; year 3 GMV above SOM; a model file that no longer matches its assumptions; contribution at or below zero. **Worth a look:** a number in the text that is in no file (years and counts of 10 or less are skipped); LTV:CAC under 3x; CAC payback over 12 months; cash that runs out inside 36 months; no breakeven; no tagline, no team, an empty pitch line.
7. Fix flags in the file the flag names, then check again until clean. Never change a number to silence a check without the founder's yes. Add a key-numbers table at the top of `seed/plan.md` and the date it was built; read back every flag and note.

Next: `seed-deck` turns the plan into ten slides; `seed-investors` uses the stage, sector, geography and raise recorded here.

## Pitfalls

- Writing a number from memory because it sounds right. Add it to its source file first, with its source.
- Silencing a check by editing the plan rather than the file that is wrong.
- Presenting projections as a forecast. Say wherever revenue or runway appears that they come from the founder's own assumptions; this is not investment advice.
- Promising funding in the plan or in the chat.
- Naming team members or roles that are not real.
- Letting the plan go stale: if the model changed after the plan was built, rebuild and re-check.

## Verification

- All nine sections are written and every pitch line fits a slide.
- The check list above is run and reported; any remaining note is shown to the founder, not hidden.
- The raise, the use of funds, TAM/SAM/SOM and the year 1-3 revenue read the same in plan, market and model files.
- The email and name in `seed/startup.md` are the founder's own.
