---
name: seed-dataroom
description: Assemble the data room investors ask for and say what is missing - the nine standard folders, a 25-item checklist marked present, missing or stale by the date in the file name or last change, an index describing every file, what to share before a term sheet and what only after, and a flag on anything that must never be in a data room (passwords, keys, ID documents, raw customer lists). Never uploads or shares anything. Use when someone asks to build a data room, what goes in one, what they are missing for diligence, what they can share now, or after a good investor call or a term sheet.
version: 1.0.0
category: planning
tags: [startup, fundraising, seed, data-room, due-diligence, checklist, documents, investors, sala-de-datos, diligencia, third-party]
status: published
source: imported
---

## When to Use

So the answer to "can you send the data room?" is a link the same day, not a week of digging. It sets up the standard folders, checks what is there against the list investors work through, warns when a file is too old to send (a cap table from last quarter, metrics from June) and writes an index. It also keeps the sequence straight: the deck and the model go out after a first meeting; the cap table, contracts and bank statements wait for a signed term sheet.

Tenth of eleven fundraising skills adapted from github.com/Jakeschincariol/seed-skill (MIT licence, © 2026 Jake Schincariol, pinned at commit fde88957238bd3bc28e80e1fe0b252a64507b108); its Python scripts are not bundled. Pipeline: seed-scout, seed-market, seed-model, seed-plan, seed-deck, seed-investors, seed-closer, seed-coach, seed-terms, seed-dataroom, seed-updates. Comes after `seed-coach` or `seed-terms`; next is `seed-updates`.

## Procedure

1. Create the nine folders under `seed/dataroom/` with `write_file` (a short README per folder is enough): 01 Company, 02 Pitch deck, 03 Financials, 04 Market, 05 Product, 06 Team, 07 Legal, 08 Cap table, 09 Customers. Tell the user what goes in each.
2. Fill what the pipeline already made: the deck exported as PDF into 02 (from `seed-deck`), `plan.md` into 01, the model and use of funds into 03, `market.md` and `competitors.md` into 04. The rest (legal, bank, cap table) comes from the user: ask, never create a document to fill a gap, and never invent one.
3. Check with `ls` and `glob` against this list. The number is how many days old a file may be before it counts as stale; a file matches an item when its name has one of the item's keywords ("deck", "cap table", "model") and it sits in that item's folder. Its date is the date in the name (`Cap table 2026-10-01.xlsx`) when there is one, else the last-changed date.

   | folder | before a term sheet | after a signed term sheet |
   | --- | --- | --- |
   | 01 Company | one-pager (90), business plan (90) | |
   | 02 Pitch deck | deck as PDF (90) | |
   | 03 Financials | 36-month model (60), use of funds (90) | historical P&L and balance sheet (45), bank statements (45) |
   | 04 Market | market sizing (180), competitor analysis (90) | |
   | 05 Product | demo (90), traction and KPIs (45), roadmap (120) | |
   | 06 Team | founder bios (365) | org chart and hiring plan (180), IP assignments |
   | 07 Legal | | incorporation and bylaws, board consents, IP, material contracts |
   | 08 Cap table | | cap table (30), signed SAFEs and notes, option plan |
   | 09 Customers | testimonials and reviews (180) | references (180), waitlist, pilots and LOIs as counts (90) |

4. Report present, missing and stale, most urgent first for the stage the user is at (`ask_user` whether a term sheet is signed). Judge by name, folder and date; do not open a file's contents unless the user asks.
5. Flag files that look like a password, a key or token, a personal ID document or a raw customer list as DO NOT SHARE, leave them out of the index, and ask the user to remove them before going on. Share counts of customers, never names and emails.
6. Write `seed/dataroom/INDEX.md`: every file, what it is, its date, size and when to share it. Files outside the nine folders are listed as loose; files the checklist does not know are listed as other and still go in the index.
7. Give the share list for the current stage: what to send now and what waits. The user picks the tool and who gets the link, preferably one view-only link per investor, and asks for an NDA before the "after" folders. If they want the link emailed, that is an email like any other: show it and send only after their explicit yes, as in `seed-closer`. With People's Hoard connected, `add_reminder` for the next refresh of anything that will go stale.

Next: `seed-updates` keeps investors warm while diligence runs and between rounds.

## Pitfalls

- Uploading, sharing or emailing a file without the user doing or confirming it.
- Creating a document to fill a gap. A missing item stays missing until the user provides it.
- Sharing the "after" folders before a term sheet is signed.
- Sending a stale file because the name looks right; check the date.
- Promising that a complete data room gets a deal closed.

## Verification

- The nine folders exist and the checklist result lists every item as present, missing or stale with its age.
- No DO NOT SHARE file appears in the index.
- `seed/dataroom/INDEX.md` lists every non-flagged file with date and share stage.
- Nothing left the machine unless the user confirmed it in the conversation.
