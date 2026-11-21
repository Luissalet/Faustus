---
name: studio-nikias-print-color
description: Colour a closed printable mesh from a coloured reference of the same pose in Nikias, check printability and export colour 3MF files with their palette. Use when a figure or base must be printed in several filaments and the colours come from an approved coloured model.
version: 1.0.0
category: engineering
tags: [studio, nikias, color, 3mf, multicolor, print, palette, filament, color-impresion, colores, figura, impresion-3d]
apps: [nikias]
accepts: [closed-print-mesh]
produces: [coloured-3mf]
status: published
source: imported
---

## When to Use

The colour step of the figure branch (`studio-figure-print-and-slice` calls it).

- **Receives:** a closed STL at final size, and a coloured reference of the same pose, scale and origin (a GLB with flat colours per part).
- **Delivers:** colour 3MF files (standard `color` and the printer-vendor format), the palette as text, three views of the coloured result, and the printability check.

## Procedure

1. **Reach the station.** `hub_app_status {app:"nikias"}`; if it is down, `hub_start_app {app:"nikias"}` and stop it (`hub_stop_app`) at the end. `hub_app_tools {app:"nikias"}` gives the live catalogue; calls are `hub_call_app {app:"nikias", tool, arguments, reason, timeout_s}`. Without the Hub's tools, the same call is `POST http://127.0.0.1:8810/api/apps/nikias/call` with `Authorization: Bearer <token>` (the Hub's data directory contains the `mcp-token` file) and body `{"tool", "arguments", "timeout_s"}`; the reply carries `ok` and `result`. With the same header, `GET /api/apps/nikias` says whether it runs (its `state` field: `running` or `down`), `POST /api/apps/nikias/start` and `/stop` start and stop it and `GET /api/apps/nikias/tools` lists its tools. Keep each call's arguments and reply as a receipt in the order folder.
2. **Project.** `project_create {source:<closed STL>, name}`.
3. **Reference.** `source_add {project_id, path:<coloured GLB>, name}`; keep `source_id`.
4. **Colour.** `color_start {project_id, mode:"model", colors:<n>, source_id}` with as many colours as the reference has (7 to 9 in the probe). Poll `job_status {job_id}`.
5. **Printability.** `print_check {project_id}`.
6. **Export.** `result_export {project_id, formats:["bambu","color"], variants:false, folder}` writes both 3MF files and the colour list.
7. **Look.** `figure_look {project_id, what:"color", views:["iso","iso_back","left"], size:500}` and open the image.
8. Stop Nikias if you started it.

## Pitfalls

- The reference and the closed mesh must share pose, scale and origin. Colour moved onto a mesh closed by another tool came out shifted (90th percentile distance 0.101 against 0.0069 when both came from the same model).
- The vendor-format 3MF crashed the slicer's command line in the probe; slice the standard `color` 3MF.
- Each colour is a filament change: merge near-identical colours before exporting.
- The printability check here does not replace the mesh inspection in Vulcan (`asset_inspect`), which counts non-manifold edges.

## Verification

- The three views opened: each colour sits on its part (cape, trousers, tube, map, base), with no bleeding across edges.
- `print_check` and Vulcan `asset_inspect {path, target:"fdm-print"}` results written in the report with their numbers.
- The palette file lists every colour that the slicer will ask for.
