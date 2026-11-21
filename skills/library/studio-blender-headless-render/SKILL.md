---
name: studio-blender-headless-render
description: Render stills of a character and set with Blender in background mode on the CPU, with framing computed by Figure Tools, the same rig and camera across variants, and a reproducible .blend per still. Use when a production order asks for a render (a back view, a lighting variant) or any station needs turn images of a mesh without opening a window.
version: 1.0.0
category: engineering
tags: [studio, blender, render, cycles, headless, background, figure-tools, camera, framing, render-sin-ventana, encuadre, iluminacion, amanecer]
accepts: [rigged_model, remodelled-scenery]
produces: [render]
status: published
source: imported
---

## When to Use

The render station of a visual order, and the turn images other stations need (`studio-vulcan-image-to-3d`, `studio-figure-print-and-slice`). For building or rigging the asset itself see `blender-asset-production`.

- **Receives:** the editable model (`.blend`, or GLBs of character, props and set in one unit), the brief (which view, which light, which details must show), and any approved earlier render to match.
- **Delivers:** PNGs, one `.blend` per still that reproduces it, and a framing file (camera location, target, lens, margin, projected bounds) per view.

## Procedure

1. **Blender.** Use the newest install under `C:\Program Files\Blender Foundation\` (5.2 in the probe). Run `blender.exe -b --factory-startup [file.blend] -P render.py -- <args>` with stdin closed and output captured to a log. Never open the interface.
2. **Engine.** `scene.render.engine = 'CYCLES'`, `scene.cycles.device = 'CPU'`, about 10 samples for checks and more for the final still. The GPUs belong to the image, video and 3D generators; do not take them for a render.
3. **Framing.** Add the directory containing the Figure Tools package to `sys.path` and call `figure_tools.core.photo_render.frame_camera(scene, objects, margin)` after placing the camera on its direction; it returns the framing to save. With `--factory-startup` add-ons are not loaded, so import what you need in the script.
4. **Variants share everything but what the order changes.** Rain and dawn of the same back view keep rig, pose and camera; only the key light colour and energy, the world colour and the visibility of the rain change. Save the `.blend` of each variant before rendering it.
5. **Render** with `scene.render.filepath` and `bpy.ops.render.render(write_still=True)`; write the framing JSON at the end and print a final marker line with the elapsed time.
6. **Turn images** of a GLB: import it, place the camera on a circle at 0, 120 and 240 degrees around its bounding box centre, render three small PNGs.

## Pitfalls

- A back view shows what was never designed: the inside face of the map, the back of a prop. Look for it and report it.
- Mixed units: GLBs in metres from the game branch, millimetres from the figure branch.
- An object hidden for one variant stays hidden for the next if the script does not reset it.
- Without the margin the feet or the top of the head get cut off.
- Four views and exports took 16 s on the CPU; much longer means too many samples or a heavy mesh.

## Verification

- Open every PNG. Check each detail to keep that should be visible from that view, the light asked for, nothing cut at the frame.
- Reopen a saved `.blend` in background mode and render it again: the image must match.
- Write in the report which still uses which `.blend` and which framing entry.
