---
name: sheet-work
description: Build and edit a spreadsheet (Hoja) the person sees and edits beside the chat, saved as .xlsx: sheet_create, sheet_read, sheet_write, sheet_query. Use when the person wants numbers, a tracker, a budget, a table with formulas, a forecast tab or a chart ("hazme una hoja con los gastos", "añade una pestaña de previsión", "haz un gráfico de esto", "build me a tracker for X", "pon una fórmula que sume").
version: 1.0.0
category: productivity
tags: [spreadsheet, hoja, excel, xlsx, formulas, chart, tracker, sheet_create, sheet_write]
status: published
source: imported
---

## When to Use

The data is tabular or computed: a tracker, a budget, a comparison, a forecast, a quick calculation that should stay live. If the output is prose, write a document; if it is spatial, use a board. A sheet has tabs, formulas that recalculate, formats and charts, and is exported as .xlsx or .csv.

## Procedure

1. Start with `sheet_create` (title, first tab). Then fill it with `sheet_write` ops in one batch: `set` writes a block of values from an address (`A1`), formulas start with `=`.
2. Write formulas, not results. Use English or Spanish names (`SUM` / `SUMA`) and refer to other tabs as `Datos!B2`. Totals, differences and projections must be formulas so the person can change an input and see the rest move.
3. Format after the data: `format` (bold, color, fill, align, and `format`: `currency` / `percent` / `date` / `number` or a code such as `'#,##0.00'`), `width`, `freeze` for the header row, `cond` for highlights, `sort` / `filter` when asked.
4. Charts: `chart` with `type` (`line`, `bar`, `pie`, `scatter`), the source `range` including the header row, and a title. Put it where it does not cover data.
5. New tabs with `add_tab`; a forecast or summary tab should read from the source tab by reference, never copy numbers.
6. Use `sheet_read` to see a range or the person's selection ("this", "these cells"), and `sheet_query` to ask a question of a tab (filter, sum, group) without loading every row.

## Pitfalls

- The person edits the same sheet at the same time. Re-read ranges they may have touched; do not overwrite their cells unless asked.
- Error values (`#DIV/0!`, `#REF!`, `#CIRC!`) are shown in the cell: after writing, read back and fix them.
- Not supported: dynamic-array spill, `OFFSET`, array constants inside `IRR`. Use helper columns instead.
- Dates are serial numbers with a date format; set the format, do not write them as text.
- Batches stop at the first op that fails and say which; fix that op and send the rest again.
- Keep headers in row 1 and one table per tab so queries and charts can find them by header text.

## Verification

`sheet_read` the filled range once after the last write: no error values, totals match a quick hand check, the chart's range is the one you meant. Tell the person what is in the sheet and which cells drive the rest, not what you meant to do.
