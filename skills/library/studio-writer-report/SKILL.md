---
name: studio-writer-report
description: Import a sourced report into a Writer document session, read it back and export it as DOCX and Markdown, then reopen the DOCX to check tables and links. Use when a production order needs a short editable report built from a brief and a list of sources, often before slides.
version: 1.0.0
category: writing
tags: [studio, writer, report, docx, markdown, sources, citations, export, informe, documento, fuentes, exportar]
apps: [writer]
status: published
source: imported
---

## When to Use

The report station of a presentation order, after the brief (`studio-drucker-engagement-brief`) and the research.

- **Receives:** `encargo.md`, and the sources as a list with an id, title, URL and the fact taken from each (`fuentes.json`).
- **Delivers:** the report as an editable DOCX and as Markdown exported by Writer, the reread text, and the source ids still attached to each claim for the slide station.

## Procedure

1. **Draft** the report in Markdown: claims with their source ids in brackets, one comparison table, a conclusion with its conditions. Every number must be in `fuentes.json`.
2. **Reach the station.** `hub_start_app {app:"writer"}` if down; calls via `hub_call_app {app:"writer", tool, arguments, reason}` (HTTP fallback `POST http://127.0.0.1:8810/api/apps/writer/call`, Bearer token from `Hoard Hub/data/mcp-token`).
3. **Session.** `wh_document_new` returns a `sessionId`. Import needs a session made this way: an invented id answers "unknown document session".
4. **Import.** Convert the Markdown to HTML (with the tables extension) and call `wh_document_import {html, sessionId}`.
5. **Read back.** `wh_document_text {sessionId}`; compare with the draft (headings, the table, every link).
6. **Export.** `wh_document_export_bytes {sessionId, format:"docx", fileName}` and again with `format:"md"`; decode the base64 field of each reply to a file.
7. **Close** with `wh_document_close {sessionId}` in a `finally`, even when a step failed.
8. **Reopen** the DOCX headless (`soffice --headless --convert-to pdf`), render its pages to PNG and look at them; count table rows and hyperlinks in `word/document.xml`.

## Pitfalls

- Writer's Markdown export escapes the brackets of source references; clean them before the next station reads the file.
- Writer does not receive the sources as citations: the ids live in the text. Hand `fuentes.json` to the slide station together with the report.
- A session left open holds memory in Writer; always close it.
- The research step cannot fill gaps here: a claim without a source goes back to research, not into the report.

## Verification

- The reopened pages show the table and the links; the counts from the XML match the draft.
- The reread text contains every source id used in the draft.
- DOCX and Markdown files saved with their sizes; the Markdown copy is the one handed on.
