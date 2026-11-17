---
name: game-unity-export
description: Export a Blender game asset to a Unity-ready FBX with a LOD chain and prove from the re-imported file that scale, axes, LOD naming and triangle counts are right. Use when an asset leaves Blender for Unity, a LODGroup is wanted, or an imported model has the wrong size or orientation.
version: 1.0.0
category: engineering
tags: [fbx, export, unity, lod, lodgroup, blender, scale, axes, exportacion, gamedev]
status: published
source: imported
---

## When to Use

An asset finished in Blender must reach a Unity project: static prop, rigged
character, or something that needs distance variants. Not for the in-engine
import settings of a character (see `unity-model-import`) and not for web
delivery (see `web-3d-asset-pipeline`).

## Procedure

1. **Fix the scene first.** One unit is one metre; transforms applied; origin at
   the base of the object (pivot between the feet for characters); materials
   named after the Unity materials they should use.
2. **Export.** From `references/`:
   `blender -b asset.blend -P export_unity.py -- --object Armor_low --out Assets/Models/Armor.fbx --lods 1,0.5,0.25 --report export.json`.
   It creates `Armor_LOD0`, `_LOD1`, `_LOD2` (Unity builds a LODGroup from
   that suffix), each a collapse decimate of LOD0 to the given share, and
   exports in metres, Y up, -Z forward, face smoothing and tangents. Rigged
   assets: export the skinned mesh with its armature from the Blender
   FBX dialog or `blender_scene` export with the `actions` list, and use
   this script only for the LOD pass on static parts.
3. **Read the report.** It re-imports the FBX into an empty scene and
   fails if there are not the expected meshes, names lack `_LOD`, triangle
   counts do not strictly decrease or the height moves more than 1% from the
   source. Record the triangle counts: they are the LOD budget.
4. **Bring it into Unity.** Drop the FBX in the project folder, then read
   the imported asset with the editor bridge (`object`, `material`): a LODGroup
   with three levels, scale factor 1, materials by name. Set LOD
   transition heights from the screen-size you can see, not from defaults.
5. **Check in the game.** Place the asset in the real scene or a playtest
   and view the LODs at the distances the camera produces. Confirm in the
   scene that textures from `game-pbr-materials` are assigned.

## Pitfalls

- Decimating LOD1 and LOD2 from LOD0 flattens thin features first; for
  characters make LOD1+ by hand around the face and hands.
- A re-import in Blender proves the file, not the Unity import: Unity may
  still apply a scale factor, merge materials or regenerate normals.
- Never copy a `.meta` file between assets; the engine owns those.
- A scale of 100 or 0.01 means centimetres were exported as metres.

## Verification

Run the export, open the report, import into Unity and read the asset back:
size in metres, LOD count, triangle counts per level, material slots. Keep
the FBX in the project folder only; scratch reports and captures stay out of
the repository.
