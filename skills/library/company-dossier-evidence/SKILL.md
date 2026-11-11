---
name: company-dossier-evidence
description: Build a company dossier or market thesis in which every claim is tied to a dated source, using frozen snapshots and backtests that state their limits. Use when a company comparison, due-diligence report or investment-style thesis must be defensible and reproducible.
version: 1.0.0
category: research
tags: [dossier, due-diligence, evidence, sources, backtest, thesis, financial-ratios, provenance, empresas, informe, investigacion, evidencia]
apps: [coase]
status: published
source: imported
---

## When to Use

You must produce a report about a company, a comparison of companies, or a thesis about a market, and a reader will ask "how do you know?". This procedure covers collecting sources, separating evidence from opinion, freezing market data and testing a thesis against history. It does not place orders or handle real money, and it does not replace professional advice.

## Procedure

1. Read the app's tool list. Its research and market modules may be switched off by default; turn on only the one you need, and say so. State the question in one sentence and the date the answer should be valid for.
2. Collect sources before writing. For each source keep the address or file, the publication date, the retrieval date and who published it. Prefer primary documents (filings, annual reports, official registries) over summaries. Import documents you were given instead of re-searching for them.
3. Extract facts into the dossier one at a time, each attached to its source. Compute ratios with the app's deterministic calculation, never by estimating in prose, and record which statement year feeds each figure.
4. Mark each statement as fact, estimate or opinion. A fact has a source and a date; an estimate names its method; an opinion is labelled as yours. Where two sources disagree, show both and say which you trust and why.
5. For market work, freeze a snapshot of the data first and build the thesis on the snapshot, so a later reader sees the same numbers. Write the thesis as something that can fail: "if X holds, Y should be above Z by date D".
6. Backtest on data the thesis was not tuned on. Hold out a period, state the fees and slippage assumed, and report the result together with its limitations (short sample, survivorship, regime change). A backtest that wins is a reason to look for the bug first.
7. Run the app's report review, which flags unsupported claims, then fix or delete what it flags. Add a limitations section that lists what was not checked.
8. Export the report and give the person the file path plus the list of sources with dates.

## Pitfalls

- Citing a page without a date; the same page says different things a year later.
- Treating an aggregator or a news summary as the primary source.
- Mixing currencies, fiscal years or accounting standards between companies in a comparison.
- Tuning a thesis on the same period used to test it.
- Presenting a model's reading of a document as the document's content; check the passage itself.
- Writing "likely" or "strong" instead of a number and a range.

## Verification

- Every sentence of fact in the report has a source with a publication date.
- Ratios were computed by the app and the inputs are listed.
- The thesis has a falsifiable condition and a held-out test with stated assumptions.
- The limitations section exists and names at least what data was missing.
- The report review reported no unsupported claim, or the remaining ones are marked as opinion.
