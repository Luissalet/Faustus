---
name: seed-deck
description: Turn the business plan into the ten-slide pitch deck investors expect (title, problem, solution, market, competition, business model, go-to-market, team, financials, the ask) with 35 words or fewer a slide, one idea a slide, a use of funds that adds up to the raise, and every number traced to the plan, model, market or competitor files; built in Cicero's Hoard when connected, otherwise as one self-contained HTML file. Use when someone asks to make a pitch deck, turn the plan into slides, check a deck, or how many words a slide should have, or after the business plan.
version: 1.0.0
category: writing
tags: [startup, fundraising, seed, pitch-deck, slides, presentation, cicero, investors, deck, presentacion, diapositivas, third-party]
status: published
source: imported
---

## When to Use

A partner flips through a deck in about three minutes; it earns a meeting, not a check. This builds the ten slides from `seed/plan.md` and the files behind it, so each slide carries one idea in a line or two and every number comes from a file.

Fifth of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-plan`; next is `seed-investors`.

## Procedure

1. Check that `seed/plan.md` was built with every check clean (see `seed-plan`) and is not older than `seed/model.md`. A plan without numbers is refused. Working files live in a `seed/` folder of the project workspace (or Cerebro notes with `brain`).
2. Outline the ten slides and read them back to the founder with their word counts:
   1. Title: name, tagline, the raise and the instrument, the founder.
   2. The problem: the problem line and the competitor review evidence ("N of M").
   3. The solution: one line and up to three points.
   4. The market: the line and TAM, SAM, SOM with sources (circles not to scale).
   5. Competition: one card per rival (rating, ratings, weak point) and yours.
   6. Business model: price, what you keep, contribution, LTV:CAC, CAC payback.
   7. Go to market: the line and up to three numbered steps.
   8. Team: the real people in the plan, with their real roles.
   9. Financials: revenue for years 1 to 3, breakeven month, peak burn; say the projections are the founder's own assumptions.
   10. The ask: the ask line, the instrument, the use of funds with shares.
   If a slide runs long, shorten the section's pitch or points in `seed/plan.md`, not the slide.
3. Ask for brand colours (or reuse the plan's). Build the deck:
   - **When Cicero's Hoard is connected** (it starts on demand; if its tools are not offered, say so and use the fallback): `deck_create` with the brief, audience (investors), tone and ten slides; `source_add` for `plan.md`, `model.md`, `market.md`, `competitors.md` so figures can only come from them; `outline_update` or `outline_generate`, then `slides_generate` or `slides_add` with speaker notes; `deck_theme` (or `theme_from_tokens` for the founder's design system).
   - **Otherwise** write one self-contained 16:9 HTML file (`seed/deck.html`) with `write_file`: inline CSS, no fonts or scripts from the web, arrow-key navigation, a print stylesheet giving one slide per page, and simple charts drawn from the numbers.
4. Lint, and fix what it finds at the source (the model, the market file), never by editing the slide:
   - 35 words or fewer in a slide's body (title, footnote and page number do not count);
   - one idea a slide: a lead of at most two sentences and at most three points;
   - the use of funds equals the raise;
   - every number shown traces to the plan, model, market, competitor or startup files (rounding is fine: $8.5B for 8,520,000,000; years and counts of 10 or less are not checked);
   - no empty slide, no market, competition, business model, financials or ask slide without its data, no team, no stale plan.
   With Cicero, `deck_check` reports numbers absent from sources and overflow; `deck_visual_review` renders each slide to check the layout before export.
5. Export for the founder: `deck_export` as PPTX and PDF, or print the HTML to PDF. Open the result with them and keep a copy for the data room.

Next: `seed-investors` builds the list to send it to; `seed-closer` links it in the outreach; `seed-coach` drills the questions it invites; `seed-dataroom` files it.

## Pitfalls

- Putting a number on a slide that lives in no file, or fixing a number in the slide instead of its source.
- Slides that say several things. If you need a second idea, you need a second slide or a cut.
- Promising funding, returns or a valuation on any slide.
- Team slide with placeholder or aspirational names.
- A generated chart whose values are not all in the sources; remove it or add the source.
- Skipping the visual check: overflow and overlap are not visible in a word count.

## Verification

- The outline shows ten slides, each at or under 35 words, with the longest named.
- The lint list is empty or every remaining finding is shown to the founder.
- Use of funds equals the raise on the last slide.
- The exported file opens and the slide count and order match the outline.
