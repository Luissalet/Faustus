---
name: studio-vulcan-image-to-3d
description: Infer a 3D mesh from character sheet views with Vulcan's image-to-3D engines, validate it, turn it on three sides and say plainly what it is good for. Use when a production order needs a first volume of a drawn character or prop and the sheet is already approved.
version: 1.0.0
category: engineering
tags: [studio, vulcan, image-to-3d, triposr, multiview, glb, gltf, mesh, imagen-a-3d, malla, volumen, modelo]
apps: [vulcan]
accepts: [character-sheet]
produces: [reference-mesh]
status: published
source: imported
---

## When to Use

The volume station of a visual order, in parallel with video once the sheet is approved. The output is a reference volume: one fused mesh with vertex colour, no skeleton and no separate parts. It is not yet the articulated model that render, game and figure need.

- **Receives:** sheet views from `studio-prospero-character-sheet`, as RGB copies without alpha (front, and back for multiview), plus the brief with height and details to keep.
- **Delivers:** a GLB at real size, its glTF validation, three turn images (0, 120, 240 degrees) and a note of what came through (silhouette, sides, props) and what did not (fused props, invented ground).

## Procedure

1. **Reach the station.** `hub_app_status {app:"vulcan"}`; if it is down, `hub_start_app {app:"vulcan"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"vulcan"}` gives the live catalogue; calls are `hub_call_app {app:"vulcan", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/vulcan/call` with `Authorization: Bearer <token>` (the Hub's data directory contains the `mcp-token` file) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/vulcan` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/vulcan/start` and `/stop` start and stop it and `GET /api/apps/vulcan/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
2. **Module.** `vulcan_modules`. The engines answer 409 `module_disabled` until `vulcan_set_module {module:"daedalus", enabled:true, automatic:false}`. Note the previous state and set it back at the end.
3. **Engines and GPU.** `daedalus_models_status` says which generators are ready. `hub_gpu_memory` to pick a card that no other job is using.
4. **Import.** `daedalus_image_import {path}` for each view; keep the returned `image_id`.
5. **Generate.** `daedalus_model_generate {image_id, title, generator:"triposr", draft_gpu:<card>, draft_resolution:128, resolution:512, remove_background:true}`. Poll `daedalus_job_get {job_id}`. About 10 s of engine time, several minutes of wall time with the queue.
6. **Multiview** when ready: `daedalus_model_generate_multiview {image_ids:[front, back], title, gpu}`. It answered 409 while its Python environment was not configured; report that instead of retrying.
7. **Export and validate.** `daedalus_model_export {job_id, size_mm:<height>, format:"glb"}`, then `gltf_validate {path}`.
8. **Turn.** Render three views of the GLB headless (`studio-blender-headless-render`) and look at them.
9. Set the module back and stop Vulcan if you started it.

## Pitfalls

- PNGs with alpha 253-255 look opaque: background removal is skipped and the backdrop becomes a block of geometry. Feed RGB copies.
- The shadow under the boots becomes a ground slab; some engines add a floor.
- Props touching the body are fused into it (the tube to the hip); hands, cape and map can merge.
- A larger engine may ask for GPU indices the Hub does not list; report the mismatch.
- A hand-built primitive model is a different lineage: never present it as the continuation of the inferred mesh. Pieces carry their origin.

## Verification

- `gltf_validate` with 0 errors; triangle count and height in the report.
- Three turn images opened: silhouette, the side of each detail to keep, and every fused or invented part, named.
- A defect goes back to the sheet station with view, part and what is wrong ("back view: cape and tube merged; need a back view with the tube clear of the cape").
- The next station (articulation) receives the GLB, the sheet and this list of fused parts.
