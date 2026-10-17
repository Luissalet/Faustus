---
name: revision-aware-media
description: Rebuild only media stages affected by an edit. Use when changing music, narration, captions or one scene in an existing production.
version: 1.0.0
category: engineering
tags: [media, cache, revision, render, produccion, musica, etapas]
status: published
source: imported
---

## When to Use

Changing a production that already has useful outputs. Preserve the last
successful delivery and relate each derived result to its actual inputs.

## Procedure

1. Read the current production revision and requested changes. Identify
   authoritative inputs and derived artifacts. Never replace an original
   asset with a render of itself as a shortcut to the requested edit.
2. Inspect the stage dependency graph. Check fingerprints for source
   content, parameters and engine versions. Reuse a result only when its
   complete dependency identity still matches the current revision.
3. Apply the smallest requested change. Music changes should invalidate
   mixing and final muxing; a scene replacement should invalidate that
   scene and its consumers. Unrelated stages remain available for reuse.
4. Render affected stages through the ordinary product queue. Preserve
   the previous successful export if a changed stage fails or is canceled.
   Report which stages were reused and which actually ran.
5. Inspect the new result and compare unaffected artifacts with their
   earlier hashes. Check duration and synchronization, since a narration
   edit can change timing even when the picture content is unchanged.
6. Save the new revision and expose its export through the normal workflow.
   Use [the local scenario](references/scenario.json) to test changes to
   music, a scene and narration independently before testing a combined edit.

## Pitfalls

- A matching filename does not establish matching content.
- A label edit should not force expensive image or voice regeneration.
- Reusing a stage after its engine or dependency changes preserves stale media.

## Verification

- The rebuilt stage set follows the dependency graph.
- Unchanged scene outputs retain their content identity.
- A failed revision leaves the last usable export intact.
- The new export contains the actual requested change with correct timing.

## Provenance

Dependency reference: https://github.com/harry0703/MoneyPrinterTurbo at
9e7d8b7efa6b912058156943906542b53b945f0a (MIT).
Original procedure for native local production tools.
