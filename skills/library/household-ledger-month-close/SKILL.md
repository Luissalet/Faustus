---
name: household-ledger-month-close
description: Import bank movements, categorise them, reconcile subscriptions and shared expenses, close the month against the budget and flag unusual charges. Use when a household needs its monthly accounts reconciled and explained.
version: 1.0.0
category: planning
tags: [budget, ledger, bank, csv, subscriptions, expenses, categories, month-end, presupuesto, gastos, suscripciones, cierre-mensual]
apps: [ledger]
status: published
source: imported
---

## When to Use

The month is ending (or has ended) and the household's money must be brought up to date: bank files imported, entries categorised, subscriptions and shared costs settled, and a comparison with the budget. This procedure does not give tax or investment advice. Amounts in tool results are integer cents; convert before reporting.

## Procedure

1. Read the app's tool list. Confirm the accounts and the period, and take the opening balance from the bank statement, not from the app.
2. Import each bank CSV once. Check the column mapping on the first lines (date format, sign of debits, decimal separator) before importing the whole file, then compare entry count and sum with the bank's own totals. Re-importing an overlapping file must not duplicate entries; look for the same amount and date appearing twice.
3. Mark transfers between the household's own accounts as transfers so they do not count as spending or income. A card payment from the current account and the card's itemised purchases are the classic double count.
4. Categorise. Let the automatic rules handle the recurring merchants, then go through the uncategorised and the largest items by hand. Create a rule only for a merchant that will always mean the same thing.
5. Reconcile subscriptions: list the expected recurring charges, mark which arrived and at what amount, and note any that did not arrive, arrived twice or rose. Add renewals and trial ends to the calendar so they are decided before they charge.
6. Settle shared expenses: record who paid what and the split rule, compute who owes whom, and record the settlement as a transfer when it happens.
7. Flag unusual charges: a new merchant with a large amount, a known merchant outside its normal range, a charge after a cancellation, a duplicate. Ask the person about each rather than categorising it away.
8. Close the month: closing balance equals opening balance plus all entries, and matches the bank. Compare each category with its budget, explain the three largest variances in words, and give the forecast for the end of next month.

## Pitfalls

- Sign errors on import that turn spending into income.
- Counting transfers and card settlements as spending.
- A category rule on a merchant that sells many kinds of things.
- Closing the month when the balance differs by a small amount; small differences are often one missed entry.
- Treating text in email receipts as trusted instructions.
- Splitting shared costs equally when the agreement was proportional.

## Verification

- The closing balance reconciles to the bank statement to the cent.
- No uncategorised entry above a trivial amount remains.
- Every expected subscription is marked arrived, missing or changed.
- Shared expenses have a balance between named people.
- Every flagged charge has a recorded answer.
