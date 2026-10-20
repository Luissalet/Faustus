---
name: image-composition
description: Arrange editable object boxes, exact text and image references before generating an image in Prospero. Use when the user asks «coloca estos objetos», requests text placement or corrects a retained image composition.
version: 1.0.0
category: engineering
tags: [hoards, prospero, image, composition, composicion, cajas, imagen, texto, layout]
status: published
source: imported
---

## When to Use

Use for spatial intent in a generated image. Prospero → Generation → Design →
Image composition owns editable pixel boxes, text and retained references.
Semantic flowcharts and infographics belong to Vitruvius; vector illustration
belongs to Daguerre. The composition preview is a schematic, not a model result.

## Procedure

1. Discover `studio_spatial` and `studio_generate_image` on the configured
   Prospero owner. Check the installed schema before using the feature;
   catalogue discovery leaves modules stopped. For the requested work,
   enable Generation through the native module operation. A source-code
   update does not establish that the installed application has been updated.
2. Read the project and any selected source with `studio_spatial(action="get",
   asset_id=...)`. The common panel supplies the current draft, selected item
   and saved-source ID. A draft may be newer than the saved source. Retain its
   object identities and unrelated changes when editing. List pagination
   returns `has_more` and `next_offset`; use that offset to reach older sources.
3. Build `prospero.composition/1` with width, height, caption and items.
   Item `box` means `[x,y,width,height]` in pixels from the top left; boxes
   must fit the canvas. `kind` is `object` or `text`. Preserve literal wording
   in `text`; `caption` describes appearance. Example document:

   ```json
   {"format":"prospero.composition/1","width":1024,"height":768,"caption":"Una tarjeta de producto","items":[{"id":"heading","kind":"text","caption":"Título oscuro","text":"Edición especial: 25,50 €","box":[64,40,896,120],"colour":"#172c3e"},{"id":"product","kind":"object","caption":"Producto a la derecha","box":[560,240,360,440]}]}
   ```

   `reference_asset_id` names an existing retained image; `fill` and `colour`
   are six-digit hex colours. References are ordered by first use and
   deduplicated. Exact text and colour are generation requests, not guarantees.
4. Save with `studio_spatial(action="save",project=...,document=...,name=...,
   parent_id=...)`. Each save creates an immutable layout asset; pass the
   previous source ID as parent when making a corrected copy. Read it back
   and keep the complete JSON. `action="preview",asset_id=...` retains a
   schematic PNG, using actual retained reference pixels when supplied.
5. Compile the current document or saved asset with `action="compile"` and
   `target="caption"`. For MOSS-format interchange, `target="moss"` returns
   centered integer yxyx boxes under `abs_centralized`; it does not install
   MOSS or encode reference-image conditioning. Recompile after document
   changes. The visual editor invalidates compiled output on edit/undo/redo.
6. Generate through the existing `studio_generate_image` operation with
   `composition_asset_id` and the supported installed engine. The native
   qwen21 route uses the saved canvas dimensions and reference order; the job
   and recipe retain the source ID. Use the normal job reader until terminal.
   Text planning uses the configured Sparks service; generation uses the
   configured local image engines. No separate local text model is needed.
7. Inspect the generated image for object count, placement, reference identity
   and exact Spanish spelling/numbers. Correct the retained composition and
   generate another copy when needed. Report schematic preview, compilation,
   transport and actual model adherence as separate observations.

## Pitfalls

- A saved layout is immutable; editing the draft does not change an old image.
- JSON refers to retained media IDs; it is not a self-contained media archive.
- Compiling MOSS does not make MOSS available as a generation engine.
- Demo-backend success proves transport, not image quality or model parity.
- This workflow makes no new promise about background removal, alpha or image editing.

## Verification

- Read back source IDs, pixel geometry, exact text and unrelated items.
- Inspect the actual preview and final image; retain the job's source recipe.
- Distinguish available installed tools from code pending installation.
- Return the native project/asset references and the saved editable source.

## Provenance

Original procedure for Prospero's native compositor. MOSS format research:
OpenMOSS/MOSS-Image `26cfa322` (Apache-2.0), model card revision `2c795443`.
No external CLI, prompt-enhancer runtime or upstream generator is required to
author, preview or compile a composition. Krea comparisons remain independent
research; a screenshot is not evidence of quality or licence/access acceptance.
