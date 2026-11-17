---
name: seed-scout
description: Find the weak points of a startup's competitors in their own one- and two-star app-store reviews - pick two to four real rivals, read up to the 500 most recent reviews of each, count the complaint themes exactly, name each rival's weak point as N of M recent low reviews mentioning it, with the rival's own pricing page as evidence, and write the result for the market, plan and deck steps. Use when someone asks who their competitors are, what customers hate about a rival, why people leave an app, or at the start of a fundraise before sizing the market.
version: 1.0.0
category: research
tags: [startup, fundraising, seed, competitors, competition, reviews, research, weak-point, scout, competidores, resenas, inversion, third-party]
status: published
source: imported
---

## When to Use

The founder has an idea (or a product) and needs the gap to win, stated with evidence an investor can check. People who already pay a rival tell a public review feed what is wrong with it, every day; this skill counts that. It reports what reviewers wrote and how often. It never makes a complaint up and never calls a rival bad in general.

First of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled, the steps run on Faustus's own tools. The pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. This one is first; next is `seed-market`. Not for products that have no public store reviews: use `deep-research` or `last-30-days-research` for those.

## Procedure

1. Read `seed/startup.md` if it exists (name, one-liner, sector, geography). Otherwise `ask_user` for the idea in a few words, the way a customer would search for it, and the country whose store to read. Working files for the whole pipeline live in a `seed/` folder of the project workspace (`write_file`, `read_file`); with no workspace, keep them as Cerebro notes (`brain`).
2. Find the rivals. `web_search` the idea in customer words, and query the store's public search (URL shapes in `references/review-feeds.md`) with `web_fetch`. Pick two to four real rivals: the most ratings and the same job. Confirm the list with the user (`ask_user`) before reading anything.
3. Read the reviews of each rival: up to 10 pages of 50, most recent first, one country, one request at a time with a pause of about a second. A feed page is large; when the `python` tool is available, fetch and tally with a few lines of standard-library code, otherwise read page by page with `web_fetch`. Record the date read and the country. Keep stars, date, title, text; drop reviewer names.
4. Count themes on the low reviews only (1 and 2 stars). Match on title and text, case-insensitive; a review may hit several themes. Starting themes: fees and pricing, cancellations and no-shows, support, bugs, trust and safety, payments and refunds, availability; add themes for the industry. M is the number of low reviews read, N the number that mention the theme. Counts are exact: tally them with `python`, never estimate by eye.
5. Name each rival's weak point. With two or more rivals it is the theme that sets it apart: its share of the app's low reviews minus the pooled share of the other apps, and the theme needs at least 5% of that app's low reviews. Support complaints show up for every app, so they rarely win. With one rival, or when asked, it is the most mentioned theme; say when the two methods disagree.
6. Read the matching reviews before quoting a theme. Keyword rules miss sarcasm and catch false hits; if a theme looks wrong, say what you saw instead.
7. Read each rival's own pricing or help page (`web_search` "<rival> fees", then `web_fetch` the rival's page) and write what it actually charges with the URL. A fee you cannot link to stays out of the plan.
8. Write `seed/competitors.md`: per rival the rating (rounded half up to one decimal) and rating count, low reviews read out of reviews read, top theme, weak point, "N of M (x.x%)", two example titles, the pricing note with its link, the date and the country. Tell the user the same, then the gap in one line: what the new product does about it.

Next: `seed-market` sizes the market; `seed-plan` and `seed-deck` quote this file; `seed-coach` turns it into investor questions.

## Pitfalls

- Writing "everyone hates X". Quote counts as "N of M recent 1-2 star reviews mention X", with the date read.
- Treating reviews as facts about the company. They are what reviewers wrote; pricing claims come from the rival's own page.
- Trusting a keyword count without reading examples, or letting the model estimate a count it did not tally.
- The feeds are public but unofficial: they can change, rate-limit or return another country. Report a failed fetch as a failure, never as "no complaints", and do not fill the gap from memory.
- Copying a stranger's review text into a deck or an email. A short title as evidence in the plan is fine.
- Logging in anywhere, posting a review, or contacting a competitor or a reviewer. None of that is part of this skill.

## Verification

- Every "N of M" can be recomputed from the reviews read, and the file states the date and country.
- Each weak point has at least two examples that were read, and the share gap against the other rivals is shown when two or more were compared.
- Every fee or price in the file has a URL to the rival's own page.
- No rating, review or fee appears that no tool returned.
