---
name: docs-drift-check
description: Check whether checked-in documents still match the code — statements about current behaviour the source contradicts, files, functions, routes or tables cited as existing that do not exist, and behaviour the code has that the document leaves out — with a fresh reader that sees only the document and the source files, through goldfish_review flow=drift. Use when about to trust an old design doc or README, after a refactor, when onboarding from documentation, or when the user asks "¿esto sigue siendo verdad?" about a document.
version: 1.0.0
category: engineering
tags: [documentation, drift, readme, design-doc, code-vs-docs, consistency, goldfish_review, documentación, deriva]
status: published
source: imported
---

## When to Use

Documentation rots quietly: a renamed function, a removed flag, a default that changed. Every later reader inherits the false statement. A drift check reads the document against the source it describes. Run it on design documents under `docs/`, READMEs of modules, API docs and runbooks, especially after a refactor or before using the document to plan new work. Not for a document that describes planned work only (files it will create are not drift).

Adapted from google/elephant-goldfish-model (Apache-2.0), skill egm-drift-report and the codebase-details reviewer, implemented by `goldfish_review`. See THIRD_PARTY_NOTICES.md.

## Procedure

1. **Pair each document with its sources.** Read the document's links and code references; list the files it describes (relative links inside its folder are added automatically). Keep the reading list to what the document covers.
2. **Run** `goldfish_review flow=drift path=<doc> context_files=[…sources]`. The reviewer reports blockers (contradicts the code; cites something that does not exist), major findings (logic, constants, environment variables, edge cases or failure handling in the code but missing from the document) and nits.
3. **Check each finding against the code yourself** before editing: the reviewer can misread. A finding about a planned file is not drift.
4. **Fix the document, not the code**, unless the user says the document is the source of truth (then it is a bug report against the code).
5. **For a folder of documents**, run one drift check per document, then summarise the documents that drifted and the number of blockers per document.
6. **Re-run** after the fixes; a document passes when no blocker or major finding remains.

## Pitfalls

- Feeding the whole repository: the reviewer drowns; give the files the document describes.
- Treating "the document does not mention X" as drift when X is out of the document's scope.
- Editing code to match an outdated document without the user deciding which is right.

## Verification

- The drift report lists each finding with the line of the document and the file it contradicts or omits.
- Every blocker was checked against the source and fixed or answered.
- The re-run has no blocker or major finding.
