---
name: data-cleaning-with-lineage
description: Ingest data, profile it, clean it with versioned steps that can be undone and replayed, apply quality rules, and build a chart or model only after the checks pass. Use when messy tabular data must become trustworthy before anyone draws conclusions from it.
version: 1.0.0
category: research
tags: [data, cleaning, profiling, lineage, quality, duckdb, charts, models, datos, limpieza, calidad, analisis]
apps: [nightingale]
status: published
source: imported
---

## When to Use

Files, folders or web tables have arrived and someone wants numbers, a chart or a forecast from them. This procedure keeps every change reproducible so the result can be defended and rerun on next month's file. It is not for streaming data or for data that must not leave a protected system. The data and calculation modules are off by default; activate only what you need.

## Procedure

1. Read the app's tool list and activate the data module explicitly. State the question the data should answer; it decides which columns matter and what "clean" means.
2. Ingest the source as it is and never edit the original file. Record where it came from and when. Check that row counts and the header match what the person expects; wrong delimiters, encodings and header offsets show up here.
3. Profile before touching anything: types per column, missing values, distinct counts, ranges, duplicates, and strange values (negative ages, future dates, a column that is half dates and half text). Write down each problem with the number of rows affected.
4. Decide a treatment per problem and why: remove, fix, impute, or keep and flag. Dropping rows changes the population, so prefer flagging when the question is about totals. Apply each treatment as one named step, so the log reads as a list of decisions. Undo any step that produced a surprise and replay the rest.
5. Add quality rules that express what must always be true after cleaning: key is unique, dates fall in the period, amounts are not negative unless refunds, categories belong to a list. Run them, read every failure, and fix the cause rather than weakening the rule.
6. Check totals against an independent number: the source's own total, a previous report, or a hand sum of a subset. Use the exact arithmetic and SQL modules for figures that will be quoted; do not estimate in prose.
7. Only now build the chart or model. Choose the chart that matches the question, label axes with units, and note filters applied. For a model, hold out data, compare against a trivial baseline, and say what the error means in the data's units.
8. Save the recipe (steps plus rules). Describe the result with the list of steps, rows removed and why, and caveats.

## Pitfalls

- Cleaning before profiling and fixing the wrong thing.
- Imputing missing values with a mean and then reporting a mean.
- Joining on a key that is not unique, which multiplies rows quietly.
- Silent type coercion, such as postcodes losing leading zeros.
- A model that beats nothing because it was never compared with a baseline.
- Charts built from a stage that was later changed.

## Verification

- Original files are untouched and every transformation is a logged step that can be replayed on a fresh copy.
- All quality rules pass, or each failure is explained in the report.
- A headline total matches an independent figure.
- Any model reports a held-out error next to a baseline.
