---
name: studio-prospero-short-video
description: Make a short clip of a scene in Prospero from an approved start frame, split into shots so the character keeps face and props, and check it frame by frame against the brief. Use when a production order asks for a few seconds of video of a character already fixed on a sheet.
version: 1.0.0
category: engineering
tags: [studio, prospero, video, image-to-video, ltx, first-last-frame, shot, clip, video-corto, plano, fotograma, escena]
apps: [prospero]
status: published
source: imported
---

## When to Use

The video station of a visual order, in parallel with the 3D station once the sheet is approved.

- **Receives:** the Prospero project and character from `studio-prospero-character-sheet`, the brief with its details to keep, and the action to film with its duration.
- **Delivers:** the MP4 (resolution, fps and duration stated), the start frame, a strip of frames sampled through the clip, and a list of what holds and what breaks, second by second.

## Procedure

1. **Reach the station** as in the sheet skill (`hub_start_app`, `hub_app_tools`, `hub_call_app {app:"prospero", ...}`), with `generation` on and one GPU job per card (`hub_gpu_memory`). `studio_video_engines` lists the installed templates.
2. **Plan shots before generating.** Write the action as beats with times. Any beat where the character turns, or a prop changes side or state, starts a new shot. One long image-to-video shot lost the face and moved the tube to the other side from second 4.5 on.
3. **Start frame.** `studio_generate_image {project, prompt, consistent:true, ...}` with `@Name` and the scene. Check the details to keep on it before animating.
4. **Each shot.** For a shot with no turn: `studio_generate_image {project, prompt, template:"ltx23_i2v", reference_asset_id:<start frame>, template_params:{length:193}, seed, reason}` gives 8 s at 24 fps (832x480 in the probe). The start frame call that worked was `{project, prompt, negative, aspect:"16:9", consistent:true, count:1, seed, reason}`. For a shot that must end on a known pose (the back view, the open map): use a first-and-last-frame template such as `wan22_flf2v` (about 5 s) with both frames taken from or made like the sheet, or a structure-controlled template such as `ltx23_control`. Poll `studio_job` every 20-30 s.
5. **Montage and sound** need the editing module; it is off by default. Turn it on with `studio_module_set` only if the order asks for a cut or sound, and set it back after.
6. **Sample frames.** Extract frames every 1.5 s and the last one (`ffmpeg -ss <t> -i clip.mp4 -frames:v 1 f<t>.png`), and build a strip. Look at every frame.
7. `studio_review_clip` exists, but it does not know the brief's details: check those yourself and write them in the report.

## Pitfalls

- Identity drift on turns: younger, rounder face; a different hairstyle.
- Props switch sides or vanish; a map becomes a small sheet with no missing corner.
- Counts and layout drift: five or six steps instead of three, the lantern on the wrong side, the character walking down instead of stopping.
- A first frame generated without `consistent:true` already loses the character.
- Clips take about two minutes each plus three for a start frame on a 16 GB card; do not queue a second GPU job on the same card meanwhile.

## Verification

- Frame strip opened and read: for each sampled time, each detail to keep is right or wrong, with the time where it breaks.
- Duration and fps measured with `ffprobe`, not assumed from the request.
- A broken beat goes back as a defect with shot, second, part and what is wrong ("4.5 s, left hip: tube now on her right"), and is regenerated as its own shot.
