---
name: game-asset-uv-bake
description: Unwrap a low-poly mesh and bake tangent-space normal and ambient-occlusion maps from a high-poly source with Cycles on the CPU. Use when a game mesh needs its detail transferred into textures, a normal map looks flat or inverted, or UV coverage needs measuring.
version: 1.0.0
category: engineering
tags: [uv, bake, normal-map, ambient-occlusion, cycles, blender, texturas, horneado, gamedev]
status: published
source: imported
---

## When to Use

The low mesh from `game-asset-retopology` (or any game mesh) must carry the
detail of a denser one. Also when an existing bake shows seams, black patches
or a flat map. Not for painting colour textures and not for baking lighting
into the scene.

## Procedure

1. **Put both meshes in one scene** at the same position and scale, the high
   one visible to the renderer (a bake ignores objects hidden from render and
   returns an empty map). Decide the map size from texel density, not by
   default: 1024 for a prop, 2048 for a hero character, powers of two.
2. **Unwrap.** The script keeps an existing UV layout and otherwise runs Smart
   UV Project. For characters, mark seams by hand first at hidden places (inner
   arm, under hair) and unwrap with those; hard-surface props can use the
   automatic layout. The report gives UV coverage and flags faces outside 0-1.
3. **Bake.** `blender -b low.blend -P bake_maps.py -- --high Sculpt --low Sculpt_low --size 1024 --outdir maps --report bake.json`
   from `references/`. It forces Cycles on the CPU, so no GPU is touched. The
   normal map is tangent space with OpenGL convention (Y up), the one Unity
   and glTF read. `--cage` sets how far above the low surface rays start,
   as a fraction of the model size; raise it when the high mesh pokes out.
4. **Judge the maps.** The report gives each map's standard deviation and the
   share of pixels that differ from the background. A normal map that is one
   flat blue, or an AO map that is all white or all black, means the rays
   missed: check the high mesh is renderable, cage distance, and that both
   meshes overlap in space.
5. **Open the maps.** Look for seam lines, stretching, bleed at island edges
   (raise `--margin`) and inverted bumps. Then pack with `game-pbr-materials`.

## Pitfalls

- Overlapping islands bake two surfaces into one place; AO and normals then
  show ghost shapes. The coverage figure does not catch overlaps by
  itself, so look at the layout.
- Smoothing groups must match between the bake and the engine, or lighting
  breaks along hard edges. Keep one convention.
- DirectX-style (Y down) normal maps look correct in some tools and
  inverted in Unity; convert with the `game-pbr-materials` script.
- Float precision on small meshes: if the model is under 10 cm in scene
  units, scale it up before baking.

## Verification

Reopen the saved PNGs, not the in-session image. Confirm size, non-flat
statistics and that the AO map shows darkening in known recesses. Apply the
maps on the low mesh in a render and compare the silhouette and shading with
the high mesh from the same camera.
