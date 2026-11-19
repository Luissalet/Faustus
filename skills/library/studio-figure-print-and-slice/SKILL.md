---
name: studio-figure-print-and-slice
description: Turn a posed character and its base into a closed printable figure at a stated size with Phidias, colour it with Nikias, inspect it with Vulcan and slice it headless with the slicer's command line, checking layers by eye. Use when a production order asks for a printable figure or diorama and its file opened in the slicer.
version: 1.0.0
category: engineering
tags: [studio, figure, print, phidias, nikias, vulcan, slicer, stl, 3mf, gcode, figura, peana, laminado, impresion-3d, cierre]
apps: [phidias, nikias, vulcan]
status: published
source: imported
---

## When to Use

The figure station, one of the three siblings from the editable model. For mesh measurement and revision comparison in general see `print-ready-3d-model`.

- **Receives:** a posed model with flat colours per part (GLB) where the figure already stands on its base: boots on the step, cape anchored to the torso, the prop held by both hands; plus the brief with the size and details to keep.
- **Delivers:** a closed STL at size, colour 3MF and palette (`studio-nikias-print-color`), Vulcan's inspection, and a sliced G-code with its log, layer count and three layer images.

## Procedure

1. **Pose for print** (in Blender, headless): join what must touch, thicken what is thinner than the nozzle allows, keep the detail to keep (the cut corner) as geometry, not texture. Export the posed GLB with colours.
2. **Close in Phidias.** `hub_start_app {app:"phidias"}` if down; calls via `hub_call_app {app:"phidias", ...}`. `project_create {source:<posed GLB>, name}`; `project_set {project_id, size_mm:160, ground:false, min_edge_mm:0.85, max_faces:400000}`; `sculpt_start {project_id}`, poll `job_status`; `close_start {project_id}`, poll `job_status`; `result_export {project_id, result:"closed", format:"stl", folder}`. About a minute on the CPU.
3. **Colour** with `studio-nikias-print-color`, using the same posed GLB as the reference.
4. **Inspect in Vulcan.** `hub_call_app {app:"vulcan", tool:"asset_inspect", arguments:{path, target:"fdm-print", lang:"es"}}` on the STL and on the colour 3MF: non-manifold edges, dimensions, wall thickness.
5. **Slice headless.** OrcaSlicer at `C:\Program Files\OrcaSlicer\orca-slicer.exe`. The command line does not resolve installed presets by name: read the machine, process and filament JSONs from `resources/profiles/<vendor>/`, merge each one with the files named in its `inherits` chain, write the flattened copies into the output folder, and pass them: `--load-settings "<machine>;<process>" --load-filaments <filament> --orient 0 --arrange 1 --outputdir <out> --slice 0 <model.3mf>`. Run it from Python with `APPDATA`, `LOCALAPPDATA`, `TEMP` and `TMP` pointing at the output folder and `creationflags=CREATE_NO_WINDOW`, logging stdout.
6. **Look at the layers.** Parse the G-code and draw three layers (bottom, the prop's height, the top) to PNG; open them.

## Pitfalls

- Colour transferred onto a mesh closed by a different tool shifts; keep one lineage from pose to colour.
- The vendor-format 3MF crashed the slicer's command line (0xC0000005); slice the standard colour 3MF.
- Vulcan does not sample wall thickness above 250,000 triangles; decimate a copy for that check or say it is unmeasured.
- The closing can fill a cut corner or merge fingers into the prop; compare against the posed model.
- The probe used one filament for the slicing test: multi-filament changes and a physical print stay unverified until someone prints.

## Verification

- STL size equals the brief; Vulcan reports 0 non-manifold edges on the STL (write the count on the 3MF too).
- Slicer exit code 0, a G-code file, layer count in the report, three layer images opened, support warnings copied.
- Views of the coloured result opened; the detail to keep is visible in the mesh.
- The report states plainly what was not checked (physical print, filament changes).
