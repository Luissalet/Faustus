---
name: obsidian-bases
description: Create, read and fix Obsidian Bases (.base files) — saved database-like views of notes with filters, formulas, sorting, grouping and summaries — and evaluate them over the Cerebro with brain_base so the same file works in Faustus and in Obsidian. Use when the user mentions Bases, a .base file, a table or cards view of their notes, "una tabla de mis notas", formulas over note properties, or wants notes filtered by tag, folder or property and shown as columns.
version: 1.0.0
category: writing
tags: [obsidian, bases, base, vault, cerebro, notes, table, cards, formulas, filters, yaml, brain_base, vista, tabla]
status: published
source: imported
---

## When to Use

A base answers "show me these notes as a table with these columns": books by status, open tasks grouped by priority, meetings of the last month with a day-of-week column. The Cerebro's vault is plain markdown, so a `.base` file in it works in Obsidian and, through `brain_base`, in Faustus (Studio → Cerebro → Bases shows it). Use it whenever the user wants a structured, repeatable view of notes instead of a one-off list. Not for a single search ("¿qué notas hablan de X?": use the `brain` tool) or for the link graph (brain_canvas_export).

Adapted from kepano/obsidian-skills (MIT), skill obsidian-bases, rewritten around the Cerebro and `brain_base`. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Scope with filters.** A base has global `filters` (and optional per-view `filters`): a string expression, or an object with exactly one key `and`, `or` or `not` holding a list. Common pieces: `file.hasTag("book")` (nested tags match: `book` also matches `book/scifi`), `file.inFolder("Projects")`, `status != "done"`, `file.mtime > now() - "7d"`, `/^\d{4}-\d{2}-\d{2}$/.matches(file.basename)`.
2. **Add formulas only when a column needs computing.** `formulas: {days_left: 'if(due, (date(due) - today()).days, "")'}`. Date minus date is a duration: read `.days` / `.hours` before rounding. Guard optional properties with `if()`. Use a formula in a view as `formula.days_left`; every `formula.X` must be defined.
3. **Write the views.** `views:` is a list of `{type: table|cards|list|map, name, order: [columns], sort: [{property, direction}], groupBy: {property, direction}, limit, summaries: {column: Sum|Average|Min|Max|Median|Range|Stddev|Earliest|Latest|Checked|Unchecked|Empty|Filled|Unique}}`. Columns: `file.name`, `file.mtime`, `file.tags`, a frontmatter property (`author` or `note.author`), or `formula.X`. Display names go in `properties: {author: {displayName: Autor}}`.
4. **Quote like YAML needs.** Single quotes around formulas that contain double quotes (`'if(done, "Sí", "No")'`); quote any string with `: # [ ] { } , & * ? | < > = ! % @`.
5. **Save and evaluate.** For a simple base, `brain_base` with `action: save`, `path: "Libros"` and a `spec` (`tags`, `folder`, `where`, `columns`, `view_type`, `group_by`, `sort_by`, `summaries`) writes valid YAML for you; for anything richer pass `yaml`. A bare name goes to `Bases/<name>.base`. Then `action: query` returns each view's rows so you can check them before telling the user it is ready.
6. **Fix, do not guess.** `action: validate` reports YAML errors, expression syntax errors, unknown view types and undefined formula references with their location; `query` also lists per-note evaluation errors. Fix the cause and query again.
7. **Embedding.** In a note, `![[Libros.base]]` or `![[Libros.base#Leyendo]]` embeds the base in Obsidian; in Faustus the view is opened from Cerebro → Bases.

## Pitfalls

- Treating a duration as a number: `(now() - file.ctime).round(0)` fails; `(now() - file.ctime).days.round(0)` works.
- A filter object with two keys (`and` and `or` side by side) is invalid: nest them.
- Referencing `formula.total` in `order` without defining `total`.
- Property names with spaces need `note["due date"]`.
- `map` views need coordinates and a plugin in Obsidian; Faustus shows them as a table.
- `this` is the base file itself on the server; there is no active note there.

## Verification

- `brain_base action=validate` returns ok with no errors.
- `brain_base action=query` shows the expected number of matching notes and the columns the user asked for.
- The base opens in Studio → Cerebro → Bases with the same rows, and the file sits in the vault where Obsidian will find it.
