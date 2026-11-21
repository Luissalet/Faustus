---
name: studio-prospero-character-sheet
description: Turn a production brief into a canonical character image, a concept plate and a multiview character sheet with a portable pack in Prospero, keeping small details such as which side an object hangs on. Use when a production order needs concept art or a character sheet that later stations (video, 3D, render) will copy.
version: 1.0.0
category: engineering
tags: [studio, prospero, character-sheet, concept-art, multiview, comfyui, image-generation, hoja-de-personaje, lamina, concepto, multivista, personaje]
apps: [prospero]
accepts: [scene-brief, concept-image]
produces: [concept-image, character-sheet]
status: published
source: imported
---

## When to Use

The concept and sheet station of a visual order. Use `character-consistency-pack` for the general method of keeping a character consistent; this skill is the exact route through Prospero that worked.

- **Receives:** the brief from `studio-writer-production-brief` (fixed traits, details to keep, place, light, style).
- **Delivers:** a Prospero project and character; a canonical image; a concept plate of the scene; the sheet views (front, three-quarter, profile, back, close-up, action) as separate PNGs plus RGB copies without alpha; a contact sheet; a `.hoardchar` pack.

## Procedure

1. **Reach the station.** `hub_app_status {app:"prospero"}`; if it is down, `hub_start_app {app:"prospero"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"prospero"}` gives the live catalogue; calls are `hub_call_app {app:"prospero", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/prospero/call` with `Authorization: Bearer <token>` (the Hub's data directory contains the `mcp-token` file) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/prospero` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/prospero/start` and `/stop` start and stop it and `GET /api/apps/prospero/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
2. **Modules and GPU.** `studio_modules`; if `generation` is off, `studio_module_set {module:"generation", enabled:true, reason}` and remember to set it back at the end. `studio_status`, then `hub_gpu_memory`: send one GPU job at a time per card. Two jobs on one card ended in out-of-memory and lost a sheet.
3. **Project and character.** `studio_create_project {name, brief, image_engine:"qwen21", reason}`. `cast_import_character {name, project, description, look}` where `look` is the fixed traits in English and `description` cites the Writer sheet.
4. **Canonical image.** `studio_generate_image {project, prompt, negative, aspect:"2:3", engine:"qwen21", count:2, seed, reason}` with `@Name`, a plain light background and even light. Write sides in picture terms as well ("the tube hangs at her left hip, on the right side of the picture") and put the wrong side in `negative`. Look at both; set the chosen one with `studio_cast {project, action:"update", kind:"character", id, fields:{canonical_asset_id}}`.
5. **Concept plate.** `studio_generate_image` with the scene prompt and `consistent:true`. A small object detail (a missing corner) does not come out of text: follow with an edit, `template:"qwen21_edit"`, `reference_asset_ids:[plate]`, a prompt that describes only the change and ends "Keep everything else exactly the same", `count:2`.
6. **Sheet.** `studio_character_sheet {character_id, views:["front","three_quarter","profile","back","closeup","action"], seed, reason}`. Poll `studio_job` every 20-30 s. If it fails, list the assets it already produced before retrying, and ask only for the missing views; a retry may redo all of them.
7. **Pack.** `studio_character_pack {action:"export", character_id, include_dataset:true, include_adapters:true, reason}`.
8. **Hand-off copies.** Prospero writes PNGs in RGBA with alpha 253-255; the 3D station then skips background removal. Save RGB copies of each view (`vista-N-<view>-rgb.png`) and a contact sheet with all views in one image.
9. Set `generation` back to its previous state and stop Prospero if you started it.

## Pitfalls

- In three-quarter and profile views the object near the camera can jump to the other side.
- Objects get duplicated (a tube and a rolled sheet).
- A failed job reports no asset ids even when some views were saved.
- Views from a failed job do not enter the pack or its dataset.
- A plate shows a prop upside down only if something in it is readable (a compass rose with a legible north).
- About one minute per 1344x768 image on a 16 GB card; budget the sheet as six images.

## Verification

- Open every image. For each view, write whether each detail to keep is right (side, missing corner, colours), and reject views that fail.
- Face, hair and clothing agree across the sheet.
- The pack contains every accepted view; RGB copies exist for the views the 3D station will use.
- Record seed, engine and prompt per image so a later order can repeat or extend it.
