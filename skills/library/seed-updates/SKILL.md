---
name: seed-updates
description: Write the monthly investor update that keeps investors warm - a TL;DR, every metric with its month-over-month growth, highlights, lowlights, asks and the runway worked out from cash and average net burn - plus a short prospects version for investors who said not now, with no cash or runway. Writes drafts and sends nothing without the founder's yes. Use when someone asks to write an investor update, a monthly update, to keep investors warm, to follow up with investors who said not now, what their runway is, or at the start of each month.
version: 1.0.0
category: writing
tags: [startup, fundraising, seed, investor-update, monthly-update, runway, burn, mom-growth, metrics, investors, informe-a-inversores, third-party]
status: published
source: imported
---

## When to Use

The habit that makes the next round easier: once a month a short, honest email on what the numbers did, what went well, what did not, what you need and how long the money lasts. Backers can help when they know the asks; investors who said "not now" watch the line go up and come back. This writes the update in the standard shape and works out the growth and the runway exactly. It writes; it does not send.

Last of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-dataroom` and then repeats every month; real results feed back into `seed-model`.

## Procedure

1. First time only: create `seed/metrics.md` (working files live in a `seed/` folder of the project workspace, or as Cerebro notes with `brain`) with the five or six numbers this company tracks every month, each with label and unit (money, count or percent), and the three headline metrics. Keep them identical month to month. Never keep an example value the founder did not give.
2. Each month, `ask_user` for the month-end numbers, the cash in the bank, any money that came in (a raise landing is `money_in`, not negative burn), and three highlights, the lowlights and up to three asks. Append one entry to `seed/metrics.md`. When People's Hoard is connected, `list_people` with `circle: "investors"` shows who to write to.
3. Work out with the `python` tool:
   - **Growth** = (this month - last month) / last month, one decimal, for money and counts ("up 23.9% from $5,325"). Percent metrics change in points ("down 0.4 pts from 2.8%"). A metric with no last month is "new this month".
   - **Net burn** = last month's cash + money in this month - this month's cash.
   - **Runway** = this month's cash / the average net burn of the last three months (fewer if that is all there is), one decimal; "to around" = this month plus the runway, rounded up to a whole month. If the cash is not running down, say so instead of giving a number.
4. Write `seed/updates/YYYY-MM.md`: subject line, TL;DR (the founder's own line, or the three headline metrics with growth and the runway), the metrics list, highlights, lowlights, asks, cash and burn and runway. A month with no lowlights, no highlights or no asks is flagged to the founder: lowlights build trust, asks get you help. Read the TL;DR, numbers and runway back to the founder and fix wrong ones in `seed/metrics.md`.
5. Prospects version, for investors who answered "not now" (`seed/pipeline.md` status passed, or People's facts): `seed/updates/YYYY-MM-prospects.md` with the headline numbers, the top two highlights, the raise from `seed/startup.md`, "Hi {first_name}," to fill in per investor and the line "Not a fit any more? Reply 'no' and I won't send these again." Cash, burn, runway, lowlights and asks stay out on purpose. Never include anyone who said no, opted out or bounced.
6. Sending: show the founder the recipients and the full email and ask once for a clear yes for that batch (`ask_user`). Then send with `send_email` from the founder's own account after `list_email_accounts`, or leave `draft_email` drafts if they prefer. Fill {first_name} per prospect. When People's Hoard is connected, `log_interaction` for each recipient (channel `email`, one line) and `add_reminder` for next month. Nothing goes out on its own and never from a scheduled task.
7. Hand off: replies go through `seed-closer` (log a reply, book a call); when real numbers drift from the plan, update `seed-model`'s assumptions.

## Pitfalls

- Sending on the founder's behalf without a yes for that batch, or through anyone else's account. Respect every no and opt-out for good.
- Rounding a number up, dropping a bad metric or skipping a lowlight so the month looks better.
- Treating a raise as negative burn, which hides the real burn and inflates runway.
- Sharing cash, burn or runway in the prospects version.
- Runway presented as a promise. It is arithmetic on the founder's own cash and burn, not investment advice.

## Verification

- Every number in the update appears in `seed/metrics.md` and growth percentages recompute from consecutive months.
- Net burn and runway are shown with their inputs (cash, money in, months averaged).
- The prospects version contains no cash, burn, runway, lowlights or asks, and carries the opt-out line.
- The recipients list excludes every no, opt-out and bounce; a sent update matches a batch the founder confirmed.
