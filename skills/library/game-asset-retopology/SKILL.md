---
name: game-asset-retopology
description: Reduce a sculpted or scanned high-poly mesh to a clean game-resolution mesh in headless Blender and measure how far it drifts from the source. Use when a mesh is too heavy for a game budget, has messy topology, or must be prepared for baking and rigging.
version: 1.0.0
category: engineering
tags: [retopology, remesh, decimate, quadriflow, lod, blender, topology, malla, gamedev]
status: published
source: imported
---

## When to Use

A mesh from a sculpt, a scan or a generator is too dense, triangulated
randomly, or not deformation-friendly. Output is a game mesh, plus the high
mesh kept as the bake source. Not for hard-surface models that are already
clean, and not for tiny props that a plain decimate handles.

## Procedure

1. **Set the budget from the project, not from habit.** Read the current
   character or prop triangle counts in the project and the platform target.
   Record the target faces before running anything.
2. **Prepare the source.** Apply transforms, remove loose parts and
   duplicates, check non-manifold edges. Quadriflow needs a closed
   shell; the script falls back to a voxel remesh followed by a decimate when
   it does not get one.
3. **Run it.** `blender -b high.blend -P retopo.py -- --source Sculpt --faces 2000 --out low.blend --report retopo.json`
   from `references/`. It creates `<source>_low`, leaves the source untouched
   and renderable, and reports faces, triangles, quad share, non-manifold
   edges and the mean and largest deviation of each mesh from the other,
   as a fraction of the bounding-box diagonal.
4. **Read the report.** The defaults fail above 1% mean or 6% largest
   deviation. The largest value usually sits on a thin feature (ear, finger,
   spike): find it before raising the limit, and raise the face count or move
   the loops by hand when a feature matters to the silhouette.
5. **Polish loops where the mesh bends.** For characters, put edge loops
   around joints and keep quads in deforming areas; hard-surface parts can
   stay triangulated. Re-run the report after every manual change.
6. **Continue** with `game-asset-uv-bake`, which needs both meshes in one
   scene.

## Pitfalls

- A decimated mesh has no loops at joints; do not rig it without checking.
- Quadriflow ignores UV seams and material borders; do those after.
- Hiding the high mesh from render makes the later bake come out empty.
- Mean deviation can pass while a whole fin disappears: look at the
  silhouette from three sides.

## Verification

Compare face counts against the budget, then open the pair side by side
and rotate. Keep the JSON and note the method that actually ran, since the
fallback changes the topology. A pass of the numbers without a look at the
silhouette is not delivered.
