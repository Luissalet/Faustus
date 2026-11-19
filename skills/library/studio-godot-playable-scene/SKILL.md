---
name: studio-godot-playable-scene
description: Assemble a small playable scene in Godot from GLB pieces (set with collision, character, a separate prop on its own pivot), drive it headless with injected input and print measured results. Use when a production order asks for a playable moment of a scene, or a station must prove that a model moves and collides as the brief says.
version: 1.0.0
category: testing
tags: [studio, godot, game, playable, headless, glb, collision, input, escena-jugable, juego, colision, prueba]
status: published
source: imported
---

## When to Use

The game station of a visual order, one of the three siblings that start from the editable model (render, game, figure). For engine-agnostic game procedures see `gamedev`.

- **Receives:** GLBs in metres: the set (from `studio-phidias-scenery`), the character, and every prop that must move on its own as a separate GLB with a known pivot. Also the brief: what the player does.
- **Delivers:** a Godot project (`project.godot`, a scene, a script), the import and run logs, and the measured values that prove each action.

## Procedure

1. **Godot.** `E:\Development\Tools\Godot-4.7.2\Godot_v4.7.2-stable_win64_console.exe` (the console build prints to stdout). Set `APPDATA` for the process to a folder inside the order, so editor settings stay out of the person's profile.
2. **Project.** Copy the GLBs next to `project.godot`. The script preloads them, instances the set and calls `create_trimesh_collision()` on every `MeshInstance3D` of the set, adds a ground body, instances the character under a node that moves, and puts the prop under its own pivot node (position and starting rotation stated).
3. **Actions.** Map keys to actions (up/W and down/S to climb, E/Space to turn the map). Movement uses the collision: cast a ray down to find the step height.
4. **Verify mode.** When `"--verify" in OS.get_cmdline_user_args()`, the script first checks that the named parts arrived (`find_child("Tubo_planos_L")`, `find_child("Mapa_esquina_cortada")`: name the parts that carry details to keep), then injects the same input a player would (an `InputEventKey` through `Input.parse_input_event`), waits for each move to finish, prints one line per measured fact (`STEP 1 y=0.18`, `MAP 3.14159 -> 0.0 rad`) and ends with a fixed marker (`GODOT_OK`) and `get_tree().quit(0)`; any failed check prints why and quits with 1.
5. **Import, then run:** `godot --headless --path <project> --import`, then `godot --headless --path <project> --quit-after 600 -- --verify`. Capture stdout to a UTF-8 file from Python (`subprocess.run(..., capture_output=True)`); a shell redirect wrote the log in UTF-16.
6. Read the log: exit code 0, the marker, and every measured value against the brief.

## Pitfalls

- A prop fused into the character mesh cannot turn; it must arrive as its own GLB. If it does not, send the defect back to the articulation station.
- Turning the prop as a separate mesh keeps its cut corner; scaling or mirroring it does not.
- Headless proves logic and collision, not looks. Say so. Looks are judged on stills from `studio-blender-headless-render` with the same GLBs; a capture of the running game needs a window, which is a desktop action reserved to the coordinator.
- Print values read from the nodes, never constants: the probe printed the map's start angle as the constant `PI`, so only its end angle was really measured. Read `rotation` before injecting the input and print both.
- Godot imports GLBs into `.godot/` on the first run; without `--import` first, the verify run may time out.

## Verification

- Exit code 0 and the marker in the run log.
- Every action of the brief has a measured line (each step height, the start and end angle of the prop) and the values make sense in metres.
- The report says what was not seen (the rendered view of the scene) and where the stills that stand in for it are.
