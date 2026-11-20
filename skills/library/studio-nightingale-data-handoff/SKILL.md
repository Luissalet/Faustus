---
name: studio-nightingale-data-handoff
description: Turn the data an order needs into a checked table or chart in Nightingale, reading the Hub's tools, other apps or a folder directly, and hand the figures to the report and slide stations with their provenance. Use when a production order asks for numbers, a table or a chart that a report or a deck will show.
version: 1.0.0
category: research
tags: [studio, nightingale, data, table, chart, provenance, handoff, hub, lineage, datos, tabla, grafico, procedencia, relevo]
apps: [nightingale]
accepts: [production-brief]
produces: [data-table, data-chart]
status: published
source: imported
---

## When to Use

The data station of a presentation or report order, before the report (`studio-writer-report`) and the slides (`studio-cicero-deck-and-rehearsal`). For the cleaning method itself see `data-cleaning-with-lineage`; this is the route that hands the figures on without anyone retyping them.

- **Receives:** the brief with the questions the figures must answer, their unit, the period, and where the data lives: a Hub tool (tasks, feed, timelines, order receipts), another app's tool, a folder or a file.
- **Delivers:** a chart id or a dataset name, its version and fingerprint from `data_handoff`, the quality report, and the analysis-log ids of the steps.

## Procedure

1. **Reach the station.** `hub_app_status {app:"nightingale"}`; start it with `hub_start_app` if it is down, and switch the data module on with `nightingale_set_module {module:"data", enabled:true}` (it is off by default). Calls are `hub_call_app {app:"nightingale", tool, arguments, reason}`.
2. **Ingest at the source.** A Hub tool: `data_ingest {kind:"hoard", app:"hoardhub", tool:"hub_agora_tasks", args:{...}, name}` (also `hub_agora_feed`, `hub_agora_timeline`, `hub_order_receipts`). Another app: the same with its app id. A folder of exports: `data_ingest {kind:"folder", path, glob:"*.json"}`; files whose columns differ are stacked by column name with `_source_file`. Never save a tool's answer to a file to ingest it.
3. **Profile and clean** with `data_profile` and `data_transform` (`preview:false` to apply; each step is a version that `data_undo` takes back). Derive the measures the questions need (`op:"derive"`), not in prose.
4. **Quality.** `data_quality {action:"define", ...}` for what must hold (no negative counts, keys unique, period covered), then `action:"run"`; read every failure.
5. **Cross-check** one total against an independent figure (the Hub's own counters, the order receipts).
6. **Chart or table.** `data_chart {action:"create", dataset, kind, x, y, agg, title}` for a chart; a table is a dataset (or a selection of its columns).
7. **Hand over.** `data_handoff {chart_id}` or `{dataset, columns, filter}` returns columns, rows and `provenance` (dataset, version, query, source, fingerprint). Give the next stations the chart id or dataset, not the numbers: Cicero takes them with `dataset_connect {kind:"nightingale", chart_id}` and `dataset_bind`; Writer with `wh_import_evidence {nightingale:{chart_id}}` and `insertDataTable`.

## Pitfalls

- A classification that is the agent's own reading (why a task came back) is not data from a source: put it in its own dataset, say so in the report, and keep it out of totals that claim to be measured.
- `data_refresh` rebuilds the version history: the version number can repeat with other figures; the fingerprint is what tells two hand-offs apart.
- A transform without `preview:false` only shows its effect.
- `to_timestamp` needs the app's `pytz` dependency; an old install answers "Required module 'pytz' failed to import".

## Verification

- `data_handoff` answers with the version and fingerprint the report and the deck cite; Cicero's snapshot origin and Writer's source show the same ref and fingerprint.
- All quality rules pass, or each failure is explained.
- One headline total matches an independent figure.
- No figure in the report or the deck was typed by an agent: each comes from a connected source.
