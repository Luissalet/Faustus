---
name: web-3d-asset-pipeline
description: Turn uploaded meshes (STL, OBJ, 3MF, GLB) into checked metrics, a layer-based fabrication estimate for quoting, and compressed GLB levels of detail for a web viewer, plus the rules for toolpath previews. Use when a web app must show, measure or quote 3D models or printing toolpaths.
version: 1.0.0
category: engineering
tags: [3d, mesh, glb, gltf, meshopt, lod, trimesh, three, r3f, toolpath, quote, presupuesto, malla, visor, impresion-3d, hormigon]
status: published
source: imported
---

## When to Use

A product page, project view or quoting flow has to display, measure or price
3D models: additive manufacturing (polymer, metal, concrete), CAD previews,
catalogue items. Also when a viewer must show printing toolpaths layer by
layer. Not for authoring geometry; Blender work belongs to
`blender-asset-production`.

## Procedure

1. **Units before anything.** STL stores no units. Ask or read them from the
   source tool, then convert the bounding box to metres and check it is
   plausible for the product (a 30 m chair means millimetres were read as
   metres). Record the assumed unit beside every derived number.
2. **Clean once, server side.** Load with trimesh (`force="mesh"`), merge
   vertices, drop degenerate and duplicate faces, remove unreferenced
   vertices and fix normals. Report counts before and after.
3. **Measure honestly.** Bounding box, surface area, watertightness, winding
   consistency, connected components. Solid volume is only meaningful for a
   watertight mesh; otherwise report the convex-hull volume and say so.
4. **Estimate fabrication by layers when quoting.** Wall-printed parts
   (concrete, large-format extrusion) are mostly hollow, so mesh volume
   overstates material. Slice horizontal sections every layer height
   (`section_multiplane`), sum each section's closed-loop perimeter, then:
   path length = sum of perimeters x walls; material volume = path x bead
   width x layer height; mass = volume x density; machine time = path / print
   speed. Flag the shortest layer time below the material's minimum layer
   time: that is a structural risk for engineering, not something the app
   decides. Prefer the real toolpath from the slicer whenever it exists.
5. **Price from the estimate with visible assumptions:** material price per
   cubic metre with a waste factor, machine rate per hour, transport and fixed
   costs as separate lines. Never show a total without its parameters.
6. **Deliver GLB.** Export the cleaned mesh as LOD0 and a simplified LOD1
   (about 25 % of faces, quadric decimation) for lists and catalogues. Then
   compress with meshopt (`gltf-transform optimize --compress meshopt`, or
   gltfpack). Meshopt decodes several gigabytes per second; Draco compresses
   slightly more but costs more CPU at load. Store under content-hashed names
   with immutable caching.
7. **Render a thumbnail at upload** so lists never load geometry.
8. **Viewer rules** (three.js / react-three-fiber):
   - load the viewer as a separate lazy chunk; show the thumbnail until
     interaction;
   - WebGL2 by default; WebGPU only after measuring (not universal on Linux
     browsers and sometimes slower);
   - `frameloop="demand"` so an idle canvas costs nothing;
   - parse and build the BVH (three-mesh-bvh) in a worker for picking and
     measurement with snapping;
   - several small viewers share one WebGL context (drei `View`), never one
     context per card.
9. **Toolpaths:** keep an intermediate format with x, y, z, layer, speed,
   flow, bead width and time; chunk by blocks of layers and decimate the
   overview on the server. Draw the overview with `LineSegments2` and the
   detail with instanced tubes extruded in the vertex shader so the bead shows
   its real width. A vertical layer slider uses `setDrawRange`; colour by
   speed or time with a legend, as slicer previews do.

The worked figures in [the scenario](references/scenario.json) let you check
an implementation against a hand calculation.

## Pitfalls

- Treating closed-mesh volume as material for a wall-printed part.
- Quoting from a mesh whose units were guessed.
- Trusting a non-watertight volume, or hiding that it is not watertight.
- Shipping one WebGL context per catalogue card; browsers cap contexts.
- Uploading a client's confidential model to a hosted converter or viewer.
- Animating the layer slider: scrubbing must answer instantly.

## Verification

- A cylinder of radius 200 mm and height 1000 mm, layer 10 mm, bead 30 mm,
  one wall, reproduces the scenario values within 0.1 % (path) and 1 %
  (volume).
- An open mesh reports `watertight: false` and no solid volume.
- Declaring metres for a millimetre file raises the units warning.
- LOD1 has under 30 % of LOD0's faces; the meshopt GLB loads in the viewer.
- The reference model reaches its first frame in under 1.5 s and orbits at
  50 fps or more on the target laptop.
