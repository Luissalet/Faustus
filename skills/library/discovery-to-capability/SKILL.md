---
name: discovery-to-capability
description: Record a discovery (repository, article, paper, video, idea), check what the family of apps already covers, propose where it belongs and link what was delivered. Use when someone shares something that might improve an app, or asks whether a capability already exists.
version: 1.0.0
category: research
tags: [discovery, research, repositories, capability-tree, proposals, provenance, dedupe, tracking, descubrimientos, investigacion, propuestas, repositorios]
apps: [darwin]
status: published
source: imported
---

## When to Use

Someone hands you a link, a paper, a video or an idea and the question is whether it is worth acting on, who should act, and whether it has been seen before. This procedure covers recording, checking existing coverage, proposing and tracking delivery. It does not cover implementing the idea in the target app; that work happens there and is only linked back.

## Procedure

1. Read the app's tool list. Before analysing anything, search the history by the canonical address and by keywords. A repository that was analysed last month should be read from its record, not analysed again.
2. If it is new, record it with where it came from (who shared it, on what date, in what context) and what kind of thing it is. Use the canonical address so two shares of the same page become one record.
3. Read the source enough to state what it does in two sentences and what is claimed versus what is demonstrated. Note licence and maintenance state when it is code; note date and venue when it is a paper.
4. Check coverage. Query the capability tree and the live tool map for the function, using several words for it, not only the source's own name. Classify: already covered, partly covered (say what is missing), not covered, or out of scope. Give the app and capability that justifies the classification.
5. Write a proposal only for partial or missing coverage. A proposal names the target app, the concrete change, the expected benefit for a real use, the cost, and a reason this beats doing nothing. If the idea belongs to no app, say that instead of forcing a fit.
6. Set the state honestly (proposed, accepted, rejected, delivered) and keep the history; a rejection with a reason prevents the same suggestion next month.
7. When work is delivered, link it: the commit, the task, the date. Verify that the delivered thing does what the proposal said before marking it delivered.
8. Report to the person: what it is, the coverage finding, the proposal if any, and the link to the record.

## Pitfalls

- Re-analysing a source that is already recorded because the search used only the title.
- Judging coverage from an app's name instead of its actual capabilities.
- Recording a claim from the source's README as if it were tested.
- Proposals that restate the source's features without saying what problem in the family they solve.
- Marking something delivered because a task closed, not because the capability exists.
- Losing the original context of who asked and why.

## Verification

- The record has a canonical address, a source context and a date.
- The coverage finding cites at least one concrete capability or tool, or says that none was found after several searches.
- Every proposal names one target app and one change.
- Delivered items link to evidence, and rejected items carry a reason.
