---
name: seed-investors
description: Research the best-fit investors for a startup from public sources only (the firm's own site, public profiles found by web search, SEC Form D filings, the press), score each by fit from 0 to 100 with the reasons and a source link on every row, and keep the ranked list as an investor pipeline in People's Hoard when connected. Use when someone asks to find investors, who to pitch, which funds back their space, to build or rank an investor list, who funded similar companies, or after the plan and deck and before outreach.
version: 1.0.0
category: research
tags: [startup, fundraising, seed, investors, vc, angels, investor-list, form-d, fit-score, people, inversores, third-party]
status: published
source: imported
---

## When to Use

A good list is short and specific: investors at your stage, in your space, with a check that fits your round, who did a relevant deal recently. Every fact keeps its source so the founder can check it before leaning on it. A high fit means a careful email is worth writing, nothing more; it does not promise a check. Not for emailing anyone: that is `seed-closer`.

Sixth of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-deck`; next is `seed-closer`.

## Procedure

1. Read `seed/startup.md` (stage, sector, geography, the raise). If any is missing, `ask_user` and write it down. Working files live in a `seed/` folder of the project workspace (or Cerebro notes with `brain`).
2. Build a comparable set from Form D filings. The US SEC's EDGAR full-text search (`efts.sec.gov/LATEST/search-index?q=<words>&forms=D`) finds companies that raised in your words and dates; each filing's `primary_doc.xml` gives amount offered and sold, number of investors, securities, date of first sale and the officers and directors. SEC asks every automated request to identify who is asking: use the founder's real name and email in the User-Agent (the `python` tool can set it; `web_fetch` cannot), pause between requests, and ask the user for that identity rather than inventing one. Form D has no business description, so search the words founders put in company names. It is a lead, not an investor list: a director who is not an officer often holds an investor's board seat, and a fund's own Form D is an investor to research.
3. Research the open web (`web_search`, `web_fetch`): "seed investors <space>", "<firm> portfolio <space>", press coverage of the comparable rounds, which names the investors who led them. Use only public pages that a search result shows. Never log in, never scrape behind a login, never scrape professional-network profiles, never collect phone numbers or home addresses; use the contact address a firm publishes for pitches.
4. For each investor write a row: name, firm, role, published email, stage, check range, sectors, geography, last relevant deal and its date, warm intro (yes/no, from whom), one-sentence `personal_line` drawn from something real they did or wrote, and every source URL read. A row with no public source URL is dropped, not ranked.
5. Score fit out of 100 and show the reason for each part:
   - stage 25 (invests at your stage; 12 if one stage off: pre-seed, seed, series A, series B, growth);
   - sectors 25 (two or more of your sectors match theirs; 15 for one);
   - check 20 (their range overlaps the check you want, by default 5% to 50% of the ask; 10 within a factor of 2; 0 if their smallest check exceeds your whole round);
   - recency 15 (their last relevant deal: 15 x (1 - days ago / 730), rounded half up);
   - geography 10 (they invest where you are, or globally);
   - warm intro 5.
6. Write the ranked table to `seed/investors.md` (fit, name, firm, check, stage, reasons, links). Check each fact you will quote in an email. Show the user the top ten with reasons and links and ask which to approach (`ask_user`).
7. When People's Hoard is connected (it starts on demand; if its tools are not offered, keep the table in `seed/investors.md`): `find_people` first so nobody is duplicated, then `upsert_person` with `circles: ["investors"]` and a short `summary` (firm and role), `add_alias` with `kind: email` for the published address, `add_fact` for stage, check range, sectors, fit, the `personal_line` and its source, and `list_people` with `circle: "investors"` to read the pipeline later. Skip anyone the user does not approve.

Next: `seed-closer` drafts one email per approved investor from this list.

## Pitfalls

- An invented investor, deal, check size or email address. Facts come from pages you read or tools returned; unknown stays unknown.
- Treating a Form D filer as an investor, or a search snippet as the page.
- Personal-style addresses and phone numbers: leave them out and say so.
- A `personal_line` that flatters instead of naming something real ("love what you're doing" names nothing).
- Adding someone to the pipeline twice because the name was not searched first; names are ambiguous, ask when `find_people` returns several.
- Presenting fit as a probability of funding.

## Verification

- Every row has a source URL and every score part has its printed reason; the total is the sum.
- No phone numbers or home addresses are stored, and each email is one the firm publishes.
- The user chose who to approach; the pipeline (file or People's circle) holds only those.
