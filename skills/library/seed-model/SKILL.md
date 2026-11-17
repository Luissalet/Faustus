---
name: seed-model
description: Build the 36-month financial model behind a raise from stated assumptions - customers, orders, GMV, revenue, contribution, payroll, marketing, opex, net and cash month by month - then report runway, breakeven month, peak burn, revenue for years 1 to 3 and the unit economics (contribution per order, CAC payback, LTV:CAC), and check that the use of funds adds up to the raise. Use when someone asks for a financial model, runway, when the business breaks even, 3-year projections, use of funds, unit economics or burn rate, or before the business plan and the deck.
version: 1.0.0
category: planning
tags: [startup, fundraising, seed, financial-model, runway, burn, breakeven, unit-economics, ltv, cac, use-of-funds, modelo-financiero, third-party]
status: published
source: imported
---

## When to Use

Investors will ask how long the money lasts, when the business pays for itself and what each customer is worth. This turns a handful of assumptions into the 36 months behind those answers, to the cent, and keeps every number traceable to an assumption the founder can defend. It is a plan from the founder's own inputs, not a forecast and not investment advice.

Third of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-market`; next is `seed-plan`.

## Procedure

1. Read `seed/startup.md` and `seed/market.md` if they exist (working files live in a `seed/` folder of the project workspace, or as Cerebro notes with `brain`). Collect the assumptions in plain words with `ask_user`, one group at a time, and write them to `seed/assumptions.md`:
   - price of one unit; `take rate` (the share of the price you keep); cost per order (percent of price and/or fixed, for example card fees);
   - customers: starting, new in month 1, monthly growth rate of new customers (a rate per year is fine), monthly churn %, orders per customer per month;
   - CAC (cost to win one customer); payroll by role with start month and an overhead % for taxes and benefits; monthly opex; one-off costs by month;
   - the raise, the month it lands, cash at start; use of funds as {item: amount}.
2. Ask where each number comes from (a pilot, a quote, a rival's public price) and note it beside the value. Guesses are fine when called guesses. Never keep an example number the founder did not confirm.
3. Compute each month with the `python` tool in exact decimal arithmetic. Customers = last month's minus churn plus new, rounded half up to whole customers. Orders = customers x orders a month. GMV = orders x price. Revenue = GMV x take rate. Gross profit (the contribution) = revenue minus the per-order costs. Opex = payroll with overhead + marketing (new customers x CAC) + monthly opex + any one-off. Net = gross profit minus opex. Cash = last month's cash + net (+ the raise in its month).
4. Derive the answers investors want:
   - **Breakeven month**: the first month with net at or above zero that every later month keeps; one good month does not count.
   - **Peak burn**: the biggest monthly loss and its month.
   - **Runway**: months until cash first goes below zero; if it never does in 36 months, say the cash lasts all 36 and give the low point.
   - **Unit economics**: revenue per order = price x take rate; contribution per order = that minus the costs of one order; CAC payback = CAC / (contribution per order x orders a month); LTV = monthly contribution per customer / monthly churn; LTV:CAC = LTV / CAC.
   - Year 1, 2 and 3 totals: orders, GMV, revenue, gross profit, opex, net, cash at year end, customers at the end.
5. Check the use of funds adds up to the raise to the cent; if not, state by how much it is short or over and fix it with the founder.
6. Show the yearly table, the unit economics, breakeven, peak burn and runway. If cash runs out before breakeven, say so plainly and name the two levers (raise more, or cut the plan: slower hiring, lower CAC). Never change an assumption on your own to make the picture better.
7. Write `seed/model.md` (assumptions, yearly table, summary, unit economics) and the 36 monthly rows as `seed/model.csv` (`write_file`, or `manage_spreadsheet` to put them in a workbook the user can open). When a workbook has formula cells without cached values, recalculate before trusting them.

Next: `seed-plan` pulls these numbers into the business plan; `seed-coach` will ask about each of them.

## Pitfalls

- Adding the raise to a month's net so burn looks smaller; the raise is cash in, not negative burn.
- Counting a single good month as breakeven.
- Hiding the bad months: show burn, the low point and a runway that ends as clearly as the good ones.
- Rounding mid-chain; round only customers and orders as stated, keep money exact.
- Changing an assumption to hit a target without the founder's yes.
- Presenting the output as a prediction or promise of funding.

## Verification

- Every figure in `seed/model.md` follows from `seed/assumptions.md`; recompute one month by hand and one yearly total.
- Use of funds equals the raise exactly.
- LTV:CAC under 3x, CAC payback over 12 months or cash running out inside 36 months are stated, not softened.
- The user confirmed every assumption, and sources or "guess" labels sit beside them.
