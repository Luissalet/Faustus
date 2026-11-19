---
name: studio-chain-visual
description: Run a visual production order from a written scene to concept art, character sheet, video, 3D, render, playable scene and printable figure, in the right order, with what each hand-off must carry and where each defect goes back. Use when someone orders a visual production of a character or scene across several stations, or when you direct the workers who do it.
version: 1.0.0
category: planning
tags: [studio, chain, pipeline, production, visual, handoff, orders, defects, cadena, produccion, relevo, concepto, video, 3d, figura, juego]
status: published
source: imported
---

## When to Use

A production order whose deliverables are pictures, clips, models or prints of a character and a place. This skill says the order of stations, what travels between them and how defects return; each station has its own skill.

## Procedure

1. **Order.** Register it: with the Hub's order tools (`hub_agora_order_create`, then `hub_agora_piece_add` for every piece) when the catalogue has them, otherwise an Agora task per station, each naming its sisters. Write the person's words unchanged and the deliverables.
2. **Brief** (`studio-writer-production-brief`). Everything downstream quotes its numbered details to keep. If the order does not come from a story, write the same fields by hand and say so.
3. **Concept and sheet** (`studio-prospero-character-sheet`). Gate: the person keeps or cancels the canonical image and the sheet (`hub_agora_gate_open` when available) before anything copies them.
4. **In parallel from the kept sheet:** video (`studio-prospero-short-video`) and volume (`studio-vulcan-image-to-3d`). The video does not wait for the 3D, nor the reverse.
5. **Editable, articulated model.** The weakest link: the inferred mesh is one fused piece without a skeleton. The next station must select, separate and repair the parts that move (the prop, the forearm), add pivots and bones, and keep the inferred mesh overlaid for comparison, marking what was rebuilt. No skill covers this yet; if nobody can do it, say so instead of hand-building a new figure and calling it the same lineage.
6. **Three siblings from the editable model,** in parallel: render (`studio-blender-headless-render`), game (`studio-godot-playable-scene`), figure (`studio-figure-print-and-slice` with `studio-phidias-scenery` and `studio-nikias-print-color`). The set comes from Phidias once and is shared: millimetres for the figure, metres for the game.
7. **Publication** only if the order includes a commercial listing.
8. **Second order on the same character** ("now from behind, at dawn"): reuse the kept sheet and the articulated model with a new camera and light before considering training an adapter; train only against a concrete image that failed.

## Hand-offs

Every hand-off carries: the piece (a file that opens), its origin (station, tool, job id, seed, input pieces), the person's decision that fixed its look, the brief's details to keep, and an earlier piece that went well through the same stations if there is one. Known losses to check at each one:

- Writer to Prospero: side conventions and small object states (write them in picture terms).
- Prospero to Vulcan: alpha 253-255 (send RGB copies); props fused to the body.
- Sheet to video: identity and props after a turn (split shots).
- Inferred mesh to articulated model: fused parts.
- Model to figure: colour lineage (same pose and origin), cut corners filled by closing.

## Defects

A defect goes back to the station that introduced it, never patched downstream: piece and revision, view or second, part, what is wrong, what was expected ("back view, left hip: tube merged with the cape"; "4.5 s: tube on her right"). Use `hub_agora_piece_defect` when available, otherwise a message in that station's task thread. A cape through a step goes back to posing; a corner lost only in slicing is fixed in the print derivative.

## Pitfalls

- Long GPU jobs: one per card; check `hub_gpu_memory`; write an Agora checkpoint with the job id so another session can resume.
- Modules switched on for a job (generation, daedalus, design) must be set back.
- Apps started for the order are stopped at the end unless another order uses them.

## Verification

- Every delivered piece has been opened and checked against each detail to keep (`hub_agora_piece_check` when available), with the result written per detail.
- Every station's output names its inputs; no piece is presented as continuing a lineage it does not come from.
- The report lists what was not verified (a print, a rendered game view).
