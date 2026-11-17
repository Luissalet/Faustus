---
name: seed-terms
description: Read a SAFE or a term sheet in plain English, work out the dilution (what a post-money SAFE owns at its cap and converts into at a priced round, a priced round's cap table with the option-pool shuffle) and flag the clauses founders most often regret, each with a severity and a question for a lawyer. Not legal advice. Use when someone asks to read a term sheet, whether a deal is good, what a SAFE or post-money cap means, how much they will own after a round, what the option pool shuffle is, or when an investor sends terms after a call.
version: 1.0.0
category: research
tags: [startup, fundraising, seed, term-sheet, safe, dilution, cap-table, option-pool, liquidation-preference, legal, hoja-de-terminos, third-party]
status: published
source: imported
---

## When to Use

The day an investor says yes and sends paper. Two jobs: the arithmetic (what a SAFE really owns, what a priced round does to the cap table, how much of "the valuation" the founders pay through a bigger option pool) and the reading (the clauses founders most often regret, in plain words, each with the question to put to a lawyer). **Not legal advice and not investment advice.** It is arithmetic and a clause reader; a startup lawyer must read every document before anything is signed, and a clause not flagged is not thereby safe.

Ninth of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-coach` and a call; next is `seed-dataroom`. The clause table is in `references/term-sheet-flags.md`.

## Procedure

1. Ask for the document and the numbers (`ask_user`): the raise, the valuation or cap, the option pool target, the current share count, other SAFEs outstanding with amount, cap and discount. Read the document with `read_file` (or `manage_documents` for an uploaded one); for a PDF use the installed PDF reader and say if text could not be extracted. Do not guess figures the document does not state.
2. Scan the clauses with `references/term-sheet-flags.md`. For each flag give the excerpt, severity (high: push back; medium: understand and likely negotiate; low: common, know it), what it means in plain words, and the question for the lawyer. List the standard terms found as standard. Never tell the user a clause is fine to sign.
3. Do the arithmetic with the `python` tool in exact fractions, rounding shares down to whole shares only at the end (real documents round the price first, so expect a few shares of drift):
   - **Post-money SAFE.** Ownership at the cap = investment / post-money cap. It converts at the lower of the cap price (cap / company capitalization: every share, option, the unissued pool and all converting SAFEs, but not the new money or a pool top-up made for the round) and the discount price (round price x (1 - discount)). With post-money caps, every extra SAFE dilutes the founders, not the other SAFE holders.
   - **Priced round.** Price per share = pre-money / fully diluted pre-money shares, where that count already includes the converted SAFEs and the new pool. The pool target is the unissued pool as a share of the post-money count; the price and the pool depend on each other, so solve the two together. That is the option-pool shuffle: the pool is carved out before the money comes in, so only existing holders pay for it. Show the effective pre-money (pre-money minus the pool top-up at the round price).
4. Show the founders' ownership before and after, the investors' share, the pool, the price per share and the effective pre-money. Also try the counter-offer (a smaller pool, a higher cap) so the user sees what each ask is worth.
5. Write `seed/terms/scan-<name>.md` and the numbers to `seed/terms/safe.md` or `round.md` (working files live in a `seed/` folder of the project workspace, or Cerebro notes with `brain`), and a short list of questions for the lawyer. Recommend a startup lawyer before signing, every time.
6. If People's Hoard is connected, record the term sheet's arrival with `log_interaction` for that investor and any dates (exploding deadline, no-shop end) with `add_reminder`. A date in the document is a deadline: put it on the calendar with `manage_calendar` when the user agrees.

Next: `seed-dataroom` for the diligence that follows a signed term sheet (what to share after); `seed-updates` keeps the other investors posted.

## Pitfalls

- Saying "sign it", or calling a term sheet good or bad overall.
- Presenting this math as the official cap table; the company's lawyer and cap-table records are the source of truth.
- Treating a clause the list did not flag as safe.
- Confusing the cap price with the discount price, or counting the new money or the round's pool top-up in the SAFE's capitalization.
- Ignoring the pool shuffle: a headline pre-money that includes a large new pool is smaller in effect.
- Inventing a share count or valuation the user did not give.

## Verification

- Each flag cites the clause text it came from.
- Ownership figures sum to 100% and reproduce from the inputs listed; one SAFE case is recomputed a second way.
- The effective pre-money is shown for any priced round with a pool.
- The reply ends with the lawyer recommendation and the question list.
