---
name: studio-cicero-deck-and-rehearsal
description: Build a sourced slide deck in Cicero from a report, check it, plan and time a rehearsal, export PPTX and PDF and reopen both to look at every slide. Use when a production order asks for slides and a timed talk from a report and a list of sources.
version: 1.0.0
category: writing
tags: [studio, cicero, deck, slides, rehearsal, speaker-notes, pptx, pdf, export, diapositivas, ensayo, presentacion, cronometro, notas]
apps: [cicero]
accepts: [editable-report, editable-deck]
produces: [editable-deck, rehearsal-notes, presentation-pptx, presentation-pdf]
status: published
source: imported
---

## When to Use

The slides and rehearsal station of a presentation order. For the general method of building a deck from a brief see `presentation-from-brief`; this is the route through Cicero that worked.

- **Receives:** `encargo.md`, the report in Markdown from `studio-writer-report`, `fuentes.json`, the talk length and the audience.
- **Delivers:** the Cicero deck id, the slide plan (`diapositivas.json`), the check result, a rehearsal plan and a timed run, PPTX and PDF files with their SHA-256, and images of the reopened slides.

## Procedure

1. **Reach the station.** `hub_app_status {app:"cicero"}`; if it is down, `hub_start_app {app:"cicero"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"cicero"}` gives the live catalogue; calls are `hub_call_app {app:"cicero", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/cicero/call` with `Authorization: Bearer <token>` (the Hub's `data/mcp-token`; on this machine `E:\Development\IA\Hoards\Faustusables\Hoard Hub\data\mcp-token`) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/cicero` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/cicero/start` and `/stop` start and stop it and `GET /api/apps/cicero/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
2. **Plan the slides** as a JSON list: `layout`, `title`, `subtitle` or body blocks, `notes` (what will be said), and `refs` (source ids). One message per slide.
3. `visual_catalog {kind:"tables"}` to pick the table layout. `deck_create {title, brief, audience, tone, language:"es", slide_count, reason}`.
4. **Sources.** `source_add {deck_id, title, text:<report>, kind:"markdown", reason}` for the report, then one `source_add {deck_id, title, text:"<fact>\n\n<url>", reason}` per source. Keep a map from source id to attachment id.
5. **Slides.** In order, `slide_add {deck_id, after_id:<previous>, layout, title, ..., notes, sources:[report attachment, ...source attachments], reason}`; append the URLs of the slide's sources to its notes.
6. **Check.** `deck_get {deck_id, detail:"full"}` and `deck_check {deck_id}`: 0 warnings; "approval pending" items are the person's gate, not an error.
7. **Rehearsal plan.** `deck_rehearsal {deck_id, duration_minutes, words_per_minute:115, pause_seconds:5, reserve_ratio:0.06}`.
8. **Timed run.** `deck_live_start {deck_id, target_seconds, main_slide_count, restart:true, reason}`, advance with `deck_live_control {deck_id, action, reason}` as each slide's notes are spoken (a local voice can read them), and read the result with `deck_rehearsal_runs`.
9. **Export.** `deck_export {deck_id, format:"pptx"}` and `format:"pdf"`; download from `download_url`, compare the SHA-256 with the receipt.
10. **Reopen.** PPTX with LibreOffice headless to PDF, PDF pages to PNG (PyMuPDF), look at every slide; count notes and editable tables in the OOXML.

## Pitfalls

- A direct bridge call and a Hub call take different body shapes; a "name: Field required" means the wrong shape.
- An objection cannot yet be rehearsed in Hypatia from here: its oral cards need a registered subject and the Hub route answered 405. Keep the objection and the answer in the notes of the slide where it will come up.
- Speaking the notes needs a voice installed on the machine; without one, time the plan only and say so.
- A deck that passes `deck_check` can still overflow a slide; only the reopened images show it.

## Verification

- Every slide image opened: title readable, table fits, sources listed.
- The timed run is under the target with its margin written down.
- PPTX and PDF hashes match the export receipts; the PPTX keeps notes and editable tables.
- Behaviour in other presentation programs stays unverified unless opened there.
