---
name: seed-market
description: Size a startup's market the way investors check it - TAM top-down (population x share x spend a year), SAM bottom-up (reachable customers x purchases a year x price) and SOM (the share of SAM winnable in N years, with a stated reason) - with a source on every input, assumptions labelled as assumptions, and SOM <= SAM <= TAM enforced. Use when someone asks how big their market is, for TAM SAM SOM, a market-size slide, whether the market is big enough, or after the competitor scout and before the financial model.
version: 1.0.0
category: research
tags: [startup, fundraising, seed, market-size, tam, sam, som, sizing, investors, mercado, tamano-de-mercado, third-party]
status: published
source: imported
---

## When to Use

The founder says "a huge market" and an investor wants three numbers they can check. TAM is everyone who could buy, top-down from public data. SAM is built bottom-up from the customers you can actually reach, how often they buy and your price. SOM is a slice of SAM you can win in a few years, with the reason written next to it. Not for forecasting revenue (that is `seed-model`).

Second of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-scout`; next is `seed-model`.

## Procedure

1. Read `seed/startup.md` and `seed/competitors.md` if they exist (working files live in a `seed/` folder of the project workspace, or as Cerebro notes with `brain` when there is no workspace). `ask_user` only for what the files do not say: who the customer is, the price, the launch geography.
2. Find the real numbers first. `web_search` for trade-association reports, census or government data, public filings; `web_fetch` the page and keep its name and URL. Use a number only if you opened the page it came from.
3. TAM, top-down: population x share x spend a year, each with a name, a value and a source. Where no public number exists, write an assumption: "assumption: <why>", and tell the user it is a guess to replace with a survey or a pilot result.
4. SAM, bottom-up: customers you can reach at launch x purchases a year x price. Match the purchase frequency and price to `seed-model`, which will reuse them.
5. SOM: a share of SAM within N years (1 to 10) and a real reason: who switches, why, how you reach them. A share over 10% of SAM needs a defence written down. Keep it consistent with the customers the model will show in year N.
6. Do the arithmetic exactly with the `python` tool (decimal arithmetic, money rounded half up to the unit) and refuse a result that breaks SOM <= SAM <= TAM. When SAM changes, recompute SOM from its share.
7. Refuse weak sources: "google", "TBD", "an AI said so", a single word, or an assumption without its reason. List every assumption each time the numbers are shown, and say so when quoting them.
8. Record headline facts the deck will quote (an industry total, a household share) as separate sourced lines, so every number on a slide has a home.
9. Write `seed/market.md`: TAM, SAM, SOM, currency, the three formulas with their inputs (name, value, unit, source, fact or assumption), the SOM share, years and reason, and the list of facts. Read the three numbers and every assumption back to the user.

Next: `seed-model` builds the 36-month model; `seed-plan` writes its Market section from this file.

## Pitfalls

- Passing off an assumption as a fact, or inflating a share to make the slide bigger.
- A TAM taken from one headline figure of "the whole industry" with no population, share and spend behind it.
- A SOM that the model later contradicts (year 3 sales above SOM). Fix the file that is wrong; do not change a number silently.
- A search snippet used as a source. Open the page.
- Mixing yearly and monthly figures, or currencies, inside one formula.
- Promising that the market turns into revenue. These are estimates from the inputs, not a forecast or investment advice.

## Verification

- The three numbers satisfy SOM <= SAM <= TAM and each can be recomputed from the listed inputs.
- Every input has a URL, a named publication or an "assumption: <why>" line; no vague sources remain.
- SOM has a one-sentence reason; any share over 10% of SAM has its defence.
- The user was told which inputs are assumptions, and `seed/market.md` is written.
