---
name: gamedev
description: Develop or revise Unity, Godot, Unreal and Three.js games using the project's actual engine and package versions. Use when changing gameplay, scenes, game assets, physics or multiplayer.
metadata:
  version: 1.0.0
  category: engineering
  status: published
  source: imported
---

## When to Use

Developing or revising an engine-based game, including gameplay code, authored
scenes, physics, rendering and local multiplayer.

## Procedure

Read the project's engine metadata before choosing APIs. Faustus exposes
`unity(action="project_profile", project="absolute project directory")` through
the agent, HTTP and MCP. This only reads local files. Other agents can inspect
the same files directly: Unity `ProjectSettings/ProjectVersion.txt` and
`Packages/manifest.json`, Godot `project.godot`, Unreal `*.uproject`, or web
`package.json` plus its lockfile. A package range is not an installed version;
Godot features are not the precise editor version.

Load the applicable reference:

- [Unity](references/unity.md): scenes, serialized editing, HDRP and Test Runner.
- [Godot](references/godot.md): node ownership, resources and runnable scenes.
- [Unreal](references/unreal.md): modules, assets and editor operations.
- [Three.js](references/threejs.md): React dependencies, physics, effects and local multiplayer.

Read existing gameplay and input code before changing the scene. Preserve the
game's controls, save data and simulation model. Identify an observable result
for the requested mechanic: a player action and its expected state, collision,
animation or network outcome. Verify in the engine and reopen the saved scene
or exported asset. Compilation alone does not establish a working mechanic.

## Pitfalls

Use installed native tools when available. Describing a node, material or
test does not mean it exists. Check the editor's returned identity and result.
Keep pending work attached to its job rather than submitting the edit again.

## Verification

Exercise the requested mechanic in the installed engine and inspect its visual
result. Reopen the saved scene or prefab to check persistence. For multiplayer,
verify two clients, reconnection and latency. Report which behavior was actually
tested and which engine capabilities are unavailable.

Workflow reference: [game development agent skills](https://github.com/gamedev-skills/awesome-gamedev-agent-skills/tree/38a4c2500eeb5ff9ac084336b8f66108069d2f8d)
(Apache-2.0). These are original local procedures; no upstream CLI, hosted
agent or generator is a runtime dependency.
