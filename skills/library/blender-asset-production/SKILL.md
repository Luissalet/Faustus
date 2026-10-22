---
name: blender-asset-production
description: Build and revise editable Blender assets through modeling, materials, rigging and export. Use when producing a 3D asset or correcting a selected scene object.
version: 1.3.0
category: engineering
tags: [blender, rigging, animation, assets, modelado, huesos, animacion]
status: published
source: imported
---

## When to Use

Producing or revising a local 3D asset. Delivery includes an editable scene
and the requested export; a screenshot alone is not a finished asset.

## Procedure

1. Read the brief, reference images, current scene and selected objects.
   Record dimensions, intended style, target engine, export format and
   required clips before choosing the modeling approach.
   Capture the current scene with native `snapshot` before a targeted edit and
   compare it afterwards with `diff(snapshot_id)`. Inspect changed field names,
   added/removed objects, shared actions and context. Its authored object-mode
   mesh/material/rig observations do not cover every `.blend` datablock or
   evaluated Geometry Nodes result; use the corresponding deep readers as well.
   Before a substantial revision, use native `checkpoint_save` with a meaningful
   label and the reference paths/review sheets used for that revision. Chat links
   its session automatically; MCP callers can supply `conversation_id`/`turn_id`.
   A checkpoint is an editable `.blend` copy; a JSON snapshot only observes state.
   `checkpoint_list` retrieves saved points. `checkpoint_restore(checkpoint_id,
   path?)` first captures the current scene and the existing destination file,
   then restores **and saves** to the working path. It resets ordinary Blender
   undo. Use the returned `undo_restore` operation to recover later manual work;
   this also captures edits made after the first restore. Preserve conversation
   history and reopen its linked session rather than deleting messages.
2. Use `blender_scene` with `action: live_probe` for the connected Blender
   5.1+ scene, and `action: official_tools` to discover the installed official
   Blender Lab tools. `official_call` takes their exact `name` and `arguments`.
   Read the bundled API/manual through those tools; query `live_submit` with
   `operation: api` for the installed RNA signatures, enum values and context
   availability before writing version-sensitive bpy code. Keep object names and stage
   outputs stable. Separate modeling, materials, rig, animation and export
   so a correction can rerun its dependencies without rebuilding everything.
3. Build the form and inspect its silhouette from the intended camera and
   additional views. Check normals, intersections and thin parts before
   spending time on materials or a bake.
   Use native `asset_check` for measured boundary/non-manifold/wire edges,
   zero-area faces, UV coverage and skin weight totals. Open surfaces and UVs
   outside 0–1 can be intentional; this report does not test intersections or
   UV overlap, which need separate inspection.
4. Create the armature and weights when required; exercise joint extremes.
   Review an animation strip as well as a still image. Long bakes and
   renders belong in recoverable local jobs with observable progress.
5. Submit native work with `live_submit(operation, arguments)`, then read
   `live_job(job_id)` until the recorded state finishes. `views` returns a
   contact sheet across explicit angles and frames. `nodes` reads or edits
   shader, geometry and compositor graphs; `animation` inspects action slots,
   NLA, curves and drivers. `recipe_save`/`recipe_run` retain named stages and
   allow a targeted stage rerun. `live_cancel` cancels pending work or stops
   between stages/views; one synchronous bpy operation must finish first.
6. Save the editable scene and export with explicit axes and units. Pass
   `actions: {"RigName": ["Idle", "Walk"]}` to the native export operation
   when delivering multiple clips. Bind clips to their owning rig, so a
   compatible rig elsewhere in the scene cannot leak unrelated animations.
   Reopen
   the export in an independent viewer and check materials, dimensions,
   bones and every requested clip. Preserve unrelated objects when editing
   a selected part.
7. Use [the local scenario](references/scenario.json) as a worked example
   of an asset brief and a targeted revision, adapting it to the real task.

## Pitfalls

- Procedural geometry does not establish quality on realistic faces.
- Material order, axes and bone naming can break an otherwise valid export.
- One good view can hide flipped faces or unbaked small UV islands.
- `.blend` reload evaluates animation at its saved frame: keyframe an intended
  override of an animated property before checkpointing. External linked
  libraries and caches remain external; inspect `external_resources` for images
  that could not be embedded. A checkpoint is not a snapshot of the process.

## Verification

- The saved scene reopens with its resources and remains editable.
- The export retains intended scale, materials and animation clips.
- Multi-view and joint-range checks cover the actual revised asset.
- The requested correction preserves unrelated geometry and behavior.

## Provenance

Workflow reference: https://github.com/ahujasid/mcp-for-blender at
7a0373ec9199183cb460068c4f96aed9c579fb4f (MIT).
Original local procedure; no asset service or commercial generator required.
Official engine: https://projects.blender.org/lab/blender_mcp release 1.0.3,
commit 2cea8d566dde07fbac28a61d698909d69724e853 (GPL-3.0-or-later).
Read-only API/scene inspection and explicit node/animation editing also draw
workflow ideas from ageless-h/blender-mcp at 19bfd0f06bebc23a06a240ac9ae731081abec9f9
(MIT), and dwgx/blender-copilot at ca345c1aea977d9103df0ceca6fcff9fe3865951 (MIT).
These are workflow references; their agent, transport and asset services are
not runtime dependencies.
