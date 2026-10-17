---
name: blender-asset-production
description: Build and revise editable Blender assets through modeling, materials, rigging and export. Use when producing a 3D asset or correcting a selected scene object.
version: 1.0.0
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
2. Discover the actual Blender capabilities. Keep object names and stage
   outputs stable. Separate modeling, materials, rig, animation and export
   so a correction can rerun its dependencies without rebuilding everything.
3. Build the form and inspect its silhouette from the intended camera and
   additional views. Check normals, intersections and thin parts before
   spending time on materials or a bake.
4. Create the armature and weights when required; exercise joint extremes.
   Review an animation strip as well as a still image. Long bakes and
   renders belong in recoverable local jobs with observable progress.
5. Save the editable scene and export with explicit axes and units. Reopen
   the export in an independent viewer and check materials, dimensions,
   bones and every requested clip. Preserve unrelated objects when editing
   a selected part.
6. Use [the local scenario](references/scenario.json) as a worked example
   of an asset brief and a targeted revision, adapting it to the real task.

## Pitfalls

- Procedural geometry does not establish quality on realistic faces.
- Material order, axes and bone naming can break an otherwise valid export.
- One good view can hide flipped faces or unbaked small UV islands.

## Verification

- The saved scene reopens with its resources and remains editable.
- The export retains intended scale, materials and animation clips.
- Multi-view and joint-range checks cover the actual revised asset.
- The requested correction preserves unrelated geometry and behavior.

## Provenance

Workflow reference: https://github.com/ahujasid/mcp-for-blender at
7a0373ec9199183cb460068c4f96aed9c579fb4f (MIT).
Original local procedure; no asset service or commercial generator required.
