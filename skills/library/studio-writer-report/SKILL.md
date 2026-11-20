---
name: studio-writer-report
description: Import a sourced report into a Writer document session, read it back and export it as DOCX and Markdown, then reopen the DOCX to check tables and links. Use when a production order needs a short editable report built from a brief and a list of sources, often before slides.
version: 1.0.0
category: writing
tags: [studio, writer, report, docx, markdown, sources, citations, export, informe, documento, fuentes, exportar]
apps: [writer]
accepts: [research-dossier, production-brief, data-table]
produces: [editable-report]
status: published
source: imported
---

## When to Use

The report station of a presentation order, after the brief (`studio-drucker-engagement-brief`) and the research.

- **Receives:** `encargo.md`, and the sources as a list with an id, title, URL and the fact taken from each (`fuentes.json`).
- **Delivers:** the report as an editable DOCX and as Markdown exported by Writer, the reread text, and the source ids still attached to each claim for the slide station.

## Procedure

1. **Draft** the report in Markdown: claims with their source ids in brackets, one comparison table, a conclusion with its conditions. Every number must be in `fuentes.json`.
2. **Reach the station.** `hub_app_status {app:"writer"}`; if it is down, `hub_start_app {app:"writer"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"writer"}` gives the live catalogue; calls are `hub_call_app {app:"writer", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/writer/call` with `Authorization: Bearer <token>` (the Hub's `data/mcp-token`; on this machine `E:\Development\IA\Hoards\Faustusables\Hoard Hub\data\mcp-token`) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/writer` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/writer/start` and `/stop` start and stop it and `GET /api/apps/writer/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
3. **Session.** `wh_document_new` returns a `sessionId`. Import needs a session made this way: an invented id answers "unknown document session".
4. **Import.** Convert the Markdown to HTML (with the tables extension) and call `wh_document_import {html, sessionId}`.
5. **Read back.** `wh_document_text {sessionId}`; compare with the draft (headings, the table, every link).
6. **Export.** `wh_document_export_bytes {sessionId, format:"docx", fileName}` and again with `format:"md"`; decode the base64 field of each reply to a file.
7. **Close** with `wh_document_close {sessionId}` in a `finally`, even when a step failed.
8. **Reopen** the DOCX headless (`soffice --headless --convert-to pdf`), render its pages to PNG and look at them; count table rows and hyperlinks in `word/document.xml`.

A table from the data station (`studio-nightingale-data-handoff`) goes into a chapter without retyping it: `wh_import_evidence {projectId, nightingale:{chart_id}}` files it as a source with one quoted claim per row and the table kept on the source; `wh_edit_writing_editorial {id, operation:{kind:"insertDataTable", sourceId}}` inserts it with its citation (undo takes it out).

## Pitfalls

- Writer's Markdown export escapes the brackets of source references; clean them before the next station reads the file.
- Writer does not receive the sources as citations: the ids live in the text. Hand `fuentes.json` to the slide station together with the report.
- A session left open holds memory in Writer; always close it.
- The research step cannot fill gaps here: a claim without a source goes back to research, not into the report.

## Verification

- The reopened pages show the table and the links; the counts from the XML match the draft.
- The reread text contains every source id used in the draft.
- DOCX and Markdown files saved with their sizes; the Markdown copy is the one handed on.
