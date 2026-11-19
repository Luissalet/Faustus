---
name: studio-phidias-scenery
description: Build the set of a scene (stairs, a doorway, a base) as a measured, remodelled model in Phidias that render, game and figure can share, and compare it with its source from two angles. Use when a production order needs a piece of scenery or a printable base in real millimetres.
version: 1.0.0
category: engineering
tags: [studio, phidias, scenery, set, remodel, base, blender, glb, obj, escenario, peana, escalera, maqueta, medidas]
apps: [phidias]
status: published
source: imported
---

## When to Use

The scenery station of a visual order. For the general remodelling method see `game-scenery-remodel`; this is the route that worked in the studio.

- **Receives:** a blockout or source mesh of the set (GLB preferred, OBJ accepted) and the brief: what the set is, how many steps, real size.
- **Delivers:** the remodelled BLEND and OBJ at a stated size in millimetres, two comparison images (profile and isometric), and a GLB in metres for the game.

## Procedure

1. **Reach the station.** `hub_start_app {app:"phidias"}` if down; `hub_app_tools {app:"phidias"}`; `status` says whether Blender is available and which jobs run. Calls via `hub_call_app {app:"phidias", tool, arguments, reason}` (HTTP fallback `POST http://127.0.0.1:8810/api/apps/phidias/call`).
2. **Source.** Build the blockout to the brief (three steps of equal rise, which side the door is). Export it as GLB: the axes are then fixed. An OBJ is read with Y up; an OBJ exported with Z up entered on its side.
3. **Project.** `project_create {source, name}`; then `project_set {project_id, size_mm:<largest dimension>, ground:false}`.
4. **Plan the parts.** `remodel_plan {project_id}` lists the parts it found. Choose a treatment per part: `remodel_set {project_id, types:"{1:{kind:keep,params:{emboss:0}}}"}` keeps part 1 as is without embossing.
5. **Run.** `remodel_start {project_id}`; poll `job_status {job_id}` (seconds on the CPU).
6. **Export.** `result_export {project_id, result:"remodel", folder}` writes BLEND and OBJ.
7. **Look.** `model_look {project_id, result:"remodel", yaw:90, pitch:12, size:600, compare:true}` (profile) and `{yaw:37, pitch:28, size:620, compare:true}` (isometric). Open both images.
8. **Game copy.** Convert the result to a GLB in metres for `studio-godot-playable-scene`; render and figure start from the same millimetre model.
9. Stop Phidias if you started it.

## Pitfalls

- Axis mix-ups between OBJ and GLB; always look at the comparison before handing off.
- Millimetres for print, metres for the game: write the unit on every hand-off.
- A set made separately from the character needs one shared origin, or the boots float above the step.
- `remodel_set` takes its `types` as a compact string, as shown; check the reply lists the treatment you meant.

## Verification

- Measured size of the result in the reply matches the brief (the probe's stairs: 168 x 186 x 54 mm).
- Profile and isometric comparisons opened: number of steps, proportions, nothing on its side.
- The game GLB imports in Godot and its collision matches the steps (checked by the game station).
