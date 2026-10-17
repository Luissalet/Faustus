---
name: historical-evidence-research
description: Reconstruct decisions using information available at their historical date. Use when evaluating an investment thesis, a backtest or a dated business decision from imported evidence.
version: 1.0.0
category: research
tags: [historical, finance, evidence, backtest, historico, inversiones, fechas]
status: published
source: imported
---

## When to Use

Reconstructing a decision as of a specific date. The central requirement is
that the evidence used was actually available then, including the correct
edition of a document and its original financial figures.

## Procedure

1. State the decision date, timezone, question and available local datasets.
   Distinguish reporting period, publication time, revision time and import
   time. A quarter ending before the decision date does not establish that
   its financial results had already been published.
2. Select source versions published by the cutoff. Keep later restatements
   and commentary available for a separate retrospective comparison. Do not
   use current fundamentals as a substitute for missing historical values.
3. Build a compact evidence packet with source identifiers, publication
   dates, extracted figures, units and the passages supporting the thesis.
   Mark missing publication dates and ambiguous availability explicitly.
4. Develop the thesis, counterarguments and scenarios from that packet.
   Record which observations would change the decision, along with the
   assumptions and costs used in any simulation. Use the configured local
   reasoning service when analysis needs a model.
5. Evaluate the historical outcome separately. Compare the original claims
   with later evidence without editing the old packet to make it appear
   prescient. Preserve a decision memory that can explain a changed view.
6. Apply [the publication scenario](references/scenario.json): eligibility
   follows the publication date, rather than the end of the fiscal period.

## Pitfalls

- Restated figures can leak future knowledge into an old decision.
- A successful return does not validate a thesis with incorrect evidence.
- Imported market files need their frequency, currency and adjustment basis.

## Verification

- Every source in the dated packet passes its publication cutoff.
- Later releases appear only in the retrospective comparison.
- Missing data stays missing and does not silently become a current value.
- The decision can be reproduced from the saved local packet.

## Provenance

Method reference: https://github.com/TauricResearch/TradingAgents at
e8f5503e9b86d5c2710b0c76b1f686703968c9df (Apache-2.0).
Original procedure for local evidence; no market API subscription required.
