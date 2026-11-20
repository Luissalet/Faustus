---
name: studio-chain-presentation
description: Run a presentation order from the client's request to brief, sourced research, report, slides, timed rehearsal and exported files that reopen, with what each hand-off carries. Use when someone orders a talk, a deck or a short report with slides, or when you direct the workers who produce it.
version: 1.0.0
category: planning
tags: [studio, chain, pipeline, presentation, report, research, slides, rehearsal, handoff, cadena, presentacion, informe, investigacion, diapositivas, ensayo]
status: published
source: imported
---

## When to Use

An order whose deliverables are a report, a deck, a rehearsed talk or all three. Each station has its own skill; this one gives the order, the hand-offs and the checks between them.

## Procedure

1. **Order.** Register it (Hub order tools when available, otherwise one Agora task per station). Copy the person's words, the deadline, the length of the talk and the audience.
2. **Brief** (`studio-drucker-engagement-brief`): `encargo.md` with deliverables and acceptance criteria, and the Drucker engagement id.
3. **Research.** Collect official sources and write `fuentes.json`: one entry per source with `id`, `title`, `url` and the exact `fact` taken. Fetch pages through Faustus on the Spark (`POST <faustus remote_url>/api/panels/fetch {url, method:"GET", timeout_s:15}`; the Hub's `/api/apps` gives `remote_url`). A company comparison with evidence belongs to Coase (`company-dossier-evidence`), whose `dossiers` module must be switched on first; Writer's link library may be off. State which of these were used.
   Figures (counts, totals, a chart) come from the data station (`studio-nightingale-data-handoff`): it reads the Hub's tools or the apps directly and hands a chart id or dataset with its provenance.
4. **Report** (`studio-writer-report`): DOCX and Markdown exported by Writer, claims carrying source ids.
5. **Slides** (`studio-cicero-deck-and-rehearsal`): one source attachment per entry of `fuentes.json` plus the report; each slide lists its sources.
6. **Rehearsal** in Cicero: plan, then a timed run under the target with its margin. Hard objections go into the notes of the slide where they will come up.
7. **Export and reopen** PPTX and PDF; the person's gate is on the reopened slides, not on the deck id.

## Hand-offs

- Brief to research: the questions the talk must answer and the decision criteria; the research answers those and nothing else.
- Research to report: `fuentes.json` travels as a file; Writer does not take citations, so ids stay in the text.
- Data to report and slides: the chart id or dataset, never the numbers; Writer files it with `wh_import_evidence nightingale` and Cicero links it with `dataset_connect kind=nightingale`, both keeping the version and fingerprint.
- Report to slides: the Markdown export (brackets cleaned) and `fuentes.json` together.
- Slides to rehearsal: notes are the script; the talk length is checked against the plan before the run.
- Rehearsal to the objection bank: not connected yet (Hypatia's oral cards need a registered subject); note it as a gap rather than faking it.

## Defects

A wrong figure goes back to the data station with the chart id and the fingerprint the slide shows; a wrong number from a document goes back to research with the slide and the source id; a slide that overflows goes back to the slide station with its image; a talk over time goes back to the plan with the slides that run long.

## Pitfalls

- Numbers that appear in slides but not in `fuentes.json`.
- Prices and availability presented as current when they are launch figures: write the date of each fact.
- Stopping at the deck id: the deliverable is files that reopen.
- Leaving test apps running; stop what you started.

## Verification

- Every number in the slides is in `fuentes.json` with its URL.
- Reopened PPTX and PDF looked at slide by slide; notes and tables present in the PPTX.
- Rehearsal time and margin written down; what was not verified (a human rehearsal, another presentation program) listed.
