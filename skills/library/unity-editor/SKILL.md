---
name: unity-editor
description: Work on a Unity project through the official Unity CLI (`unity`) and the running Editor instead of hand-editing scenes, prefabs and .meta files. Use when the task touches a Unity game or scene — GameObjects, components, prefabs, animators, cameras, colliders, input, play-mode checks, builds or tests.
version: 1.0.0
category: engineering
tags: [unity, gamedev, csharp, editor, cli]
status: published
source: imported
---

## When to Use

The workspace holds a Unity project (an `Assets/` and a `ProjectSettings/` folder side by side), or the user talks about a Unity scene, prefab, GameObject, component, animator, camera rig or build. `unity` is on PATH (on Windows it installs to `%LOCALAPPDATA%\Unity\bin\unity.exe`). Use `--format json` whenever you read its output.

## Procedure

1. Find the project folder: the one that contains `Assets/` and `ProjectSettings/` (it can be nested, e.g. `Game\Game\Game`). Call it PROJECT and pass `--project-path "PROJECT"` to every Editor command.
2. `unity status --json --project-path "PROJECT"`. An instance in state `ready` means the Editor is open and drivable.
3. No instance:
   - the project lacks the Pipeline package → `unity pipeline install --project-path "PROJECT"` (adds `com.unity.pipeline` to `Packages/manifest.json`; an open Editor imports it when it regains focus);
   - the Editor is closed → `unity open "PROJECT"`, then `unity status --until-ready --project-path "PROJECT"`;
   - still nothing → compile errors put the Editor in Safe Mode: `unity pipeline list --project-path "PROJECT"`, read the errors, fix the C#, restart the Editor.
4. `unity command --project-path "PROJECT"` lists what the Editor exposes; `unity list` shows the Pipeline tools.
5. Change the scene by running C# in the Editor: `unity command eval '<C# statements>' --project-path "PROJECT" --json`. The code is a statement list: end it with `return <value>;` to read something back (e.g. `return UnityEngine.SceneManagement.SceneManager.GetActiveScene().rootCount;`); the value comes back in `data.result.result`. From PowerShell, avoid double quotes inside the C# (or write it to a file and pass its content). Use `UnityEditor` APIs (`PrefabUtility`, `AssetDatabase`, `EditorSceneManager`, `Undo`) and save with `EditorSceneManager.SaveOpenScenes()` / `AssetDatabase.SaveAssets()`.
6. Grouping objects under an empty parent: `var p = new GameObject("Steps");` then `child.transform.SetParent(p.transform, true)` for each one (world position kept).
7. A playable character: the root holds the `CharacterController` (or `Rigidbody` + `CapsuleCollider`); the visual model is its CHILD at local (0,0,0) with the feet on the root's pivot; the collider's `center.y` is half its height and the height matches the model's `Renderer.bounds`. An imported model needs an `Animator` whose controller has an Idle state with a clip; for an FBX, check `ModelImporter.animationType` and `importAnimation` and extract the clips.
8. Camera-relative movement: `var f = Vector3.ProjectOnPlane(cam.forward, Vector3.up).normalized; var r = Vector3.ProjectOnPlane(cam.right, Vector3.up).normalized; move = f * input.y + r * input.x;` and turn the character towards `move`.
9. After writing or changing any C#: `unity recompile --project-path "PROJECT" --json` and fix every error before going on.
10. Tests and builds: `unity test "PROJECT" --json`; `unity build "PROJECT"` (batch mode — not while that project's Editor is open).

## Pitfalls

- Hand-written `.unity` / `.prefab` / `.controller` / `.meta` YAML: broken references, a model displaced from its collider, a character sunk into the floor, an Animator with no clips. Let the Editor write them.
- Several Editors open: without `--project-path` the CLI targets the Editor of the current directory.
- Changes made while in Play mode are thrown away when Play mode ends: stop playing before editing.
- An Editor that is still importing answers slowly: give the command a longer `--timeout` instead of retrying in a loop.
- `unity bug` sends logs to Unity: never run it without asking.

## Verification

1. `unity command editor_play --project-path "PROJECT"`, wait a few seconds and confirm frames advance (an unfocused Editor can freeze at frame 1 while reporting it is playing).
2. Look at the result: capture the Game view or take a desktop screenshot and inspect it.
3. Read the Console for errors and warnings.
4. Stop Play mode and report what changed (objects, components, scripts, paths) and what the check showed.

`unity skill show` prints the full official guide; `unity skill show --list` lists its references (build-run-test, integration-advanced, playmode-verification-loop). `unity docs <Class>` opens the API page for the project's Editor version.
