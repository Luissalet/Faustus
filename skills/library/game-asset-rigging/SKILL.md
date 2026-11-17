---
name: game-asset-rigging
description: Rig a character or creature mesh with a Unity Humanoid-compatible skeleton and clean skin weights in headless Blender, then audit the result. Use when a game asset needs a rig, an imported mesh deforms badly, or a rig must map to Unity's Humanoid avatar.
version: 1.0.0
category: engineering
tags: [rigging, armature, skinning, weights, blender, unity, humanoid, esqueleto, personaje, gamedev]
status: published
source: imported
---

## When to Use

A mesh has to move: a new character, a replaced body, or an asset whose
deformation breaks at the shoulders or hips. Also when Unity's avatar setup
reports missing bones. Not for authoring the animation itself (see
`game-motion-from-video` and `blender-asset-production`) and not for engine
import settings (see `unity-model-import`).

## Procedure

1. **Read the mesh before rigging.** Check height in metres, that it faces
   -Y with Z up, that transforms are applied, and that arms are spread in a
   T or A pose. A rest pose with arms down makes automatic weights bleed
   between torso and arm; fix the pose first, not the weights.
2. **Build the skeleton.** Run `references/rig_humanoid.py` headless:
   `blender -b model.blend -P rig_humanoid.py -- build --mesh Body --out rigged.blend --report rig.json`.
   It places hips, spine, chest, neck, head and both arms and legs from the
   bounding box, using the `Left`/`Right` prefix names that Unity maps without
   manual work. The placement is a first pass: open the scene, move each
   joint to the real articulation (knee, elbow, wrist) in Edit mode and keep
   the names. Add fingers, toes or tail bones only as children of the existing
   chain.
3. **Skin with automatic weights, then clean.** The script limits every
   vertex to four influences (the default Unity skin-weight budget) and
   normalises. Check the joints by posing extremes: shoulder to 90 degrees,
   knee to 120, neck to 45. Paint only where a pose shows collapse.
4. **Audit.** `blender -b rigged.blend -P rig_humanoid.py -- audit --report rig.json`
   must report no missing required bones, zero unweighted vertices, none above
   four influences and a weight-sum error below 0.001. With the native Blender
   tools, `asset_check` adds skin-weight totals and `diff` against a
   `snapshot` shows only what changed.
5. **Hand over.** Export with `game-unity-export`, then confirm in Unity that
   the avatar configures as Humanoid with every required bone green.

## Pitfalls

- Auto weights on overlapping parts (an armour plate over a limb) assign the
  plate to the wrong bone; weight such parts as separate meshes or by hand.
- Renaming bones after animation breaks every action that targets them.
- A mesh not at the origin, or scaled non-uniformly, gives a skeleton that
  looks right and exports sideways. Apply transforms first.
- The audit proves weight hygiene, not good deformation; only a posed view
  shows that.

## Verification

Keep the JSON report and a contact sheet of the extreme poses from three
angles. Reopen the saved `.blend` and run the audit again: a rig that passes
only in the session that built it is not delivered. Name the poses you tested
and the ones you did not.
