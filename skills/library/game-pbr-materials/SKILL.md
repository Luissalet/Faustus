---
name: game-pbr-materials
description: Check and pack PBR texture sets (base colour, normal, roughness, metallic, occlusion) into Unity HDRP mask maps or glTF occlusion-roughness-metallic maps. Use when textures arrive in separate files, a material looks too dark, too shiny or inverted in the engine, or channels must be packed.
version: 1.0.0
category: engineering
tags: [pbr, materials, textures, hdrp, gltf, mask-map, roughness, metallic, materiales, gamedev]
status: published
source: imported
---

## When to Use

A set of maps from a bake, a texture painter or a scan must become engine
materials. Also when colours look washed out or crushed, metal looks like
plastic, or the normal map lights from the wrong side. Not for generating
textures from scratch.

## Procedure

1. **Gather and name the set.** One folder per material; same size for every
   map; square powers of two (512, 1024, 2048). Record whether the normal map
   is OpenGL (Y up) or DirectX (Y down): look at a bump lit from the top; if
   it reads as a dent, it is the other one.
2. **Check and pack.** From `references/`:
   `python pbr_pack.py --albedo a.png --normal n.png --roughness r.png --metallic m.png --ao ao.png --normal-convention directx --target hdrp --outdir packed --name Armor --report pbr.json`.
   It needs numpy and Pillow. Nothing is written when a check fails.
   Checks: equal square power-of-two sizes; albedo luminance within
   30-240 on non-metal pixels (real surfaces are neither black nor white);
   unit-length normals pointing out of the surface; metallic mostly 0 or 1.
3. **Use the output.** Target `hdrp` writes `_BaseColor`, `_Normal` and
   `_Mask` (R metallic, G occlusion, B detail mask, A smoothness = 1 -
   roughness). Target `gltf` writes `_ORM` (R occlusion, G roughness, B
   metallic). In Unity import the base colour as sRGB; the mask and the normal map are
   linear data (sRGB off), and the normal map's texture type is Normal map.
4. **Assign to the HDRP/Lit material** with the existing material tools
   (`material`, `modify_material`): base map, mask map, normal map, and set
   the metallic and smoothness remaps to full range so the texture, not the
   slider, decides.
5. **Judge in the engine,** under the project's own lighting, not on a preview
   sphere only: a bright sun and a dim interior both.

## Pitfalls

- Roughness and smoothness are inverses; a roughness map used as the alpha
  channel makes everything shiny where it should be rough.
- Intermediate metallic values on a painted edge are blends, and look
  grey; keep metal bright and non-metal dull, and let edges be 0 or 1.
- Emission is not packed here: keep it as its own map and set the HDRP
  exposure weight in the material.
- Height maps for parallax or tessellation are separate and cost shader
  time; add them only when the silhouette needs them.

## Verification

Reload the packed PNGs and read each channel: the mask's alpha must equal
1 - roughness, the green channel the occlusion, the red the metallic. Then
open the material in the engine and compare it with the source images
for tint, specular and bump direction.
