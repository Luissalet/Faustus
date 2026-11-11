---
name: print-ready-3d-model
description: Take a 3D model through measurement, revision comparison, relief or extrusion work and export, with the checks that matter before it goes to a printer. Use when a mesh must be prepared, compared with an earlier version or turned from a picture into something printable.
version: 1.0.0
category: engineering
tags: [3d-printing, mesh, stl, 3mf, relief, extrusion, measurement, revision, impresion-3d, malla, modelado, medidas]
apps: [vulcan]
status: published
source: imported
---

## When to Use

You have a model or a picture and need a mesh that prints correctly: sizes known, differences between versions understood, a relief or an extrusion made, and a clean export. It does not choose slicer settings for a particular printer or promise a successful print; it removes the avoidable causes of failure. Modules start switched off: start the app and switch on only the module for the step (`vulcan_set_module`), and read the tool list to see the native names.

## Procedure

1. Open the model in the library and measure it: overall dimensions in millimetres, units as imported (a model in metres imported as millimetres is tiny), volume, number of parts, and whether it is a single closed solid. Write the target size and check the measurements against it.
2. If a newer revision exists, compare it with the older one using the physical comparison, not by eye. The tool aligns them by rigid registration and reports distance between surfaces; read the largest deviations and where they are, and decide whether each is intended.
3. For a figure relief, decide the height range from the printer's resolution and the smallest feature to keep. Generate the relief, then look at it from a low angle with side light; flat heights hide errors that raking light shows.
4. For a picture-to-3D or silhouette extrusion, clean the picture first (clear background, solid shape, no holes unless wanted). Choose the thickness from the use: a keyring needs a minimum thickness that survives handling. Check that thin bridges and islands are connected.
5. Check printability: watertight surface with no open edges, no inverted normals, no self-intersections, wall thickness above the minimum for the nozzle and material, overhangs that need support, parts that float free, and holes small enough to close when printed. Fix at the source model when possible.
6. Scale and orient for printing: largest flat face on the bed, features that carry load aligned with layers, text raised on top rather than on a side. Split a model that exceeds the bed and add alignment pegs.
7. Export in the format the printer software takes (STL for geometry, 3MF when units and multiple parts matter), then reopen the exported file and measure it again to confirm size and unit.
8. Hand over with a short note: size, orientation, supports required, known risks such as thin parts.

## Pitfalls

- Unit errors that show up only when the slicer says the part is 0.3 mm tall.
- Fixing holes automatically with no look at what was filled.
- Comparing revisions without alignment, so every point appears to have moved.
- Relief heights below printer resolution.
- Thin walls that pass the geometry check but fail on the nozzle.
- Exporting before the final scale.

## Verification

- Final dimensions in millimetres are recorded and match the target.
- The mesh is closed, with no self-intersections reported.
- Wall thickness and smallest feature were compared with the printer's limits.
- The exported file was reopened and measured.
- Intended and unintended differences between revisions are separated.
