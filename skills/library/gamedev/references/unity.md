# Unity

Use the version from ProjectVersion.txt and installed package APIs. Render
pipeline settings and shader property names differ across HDRP, URP and built-in.
Blood Arrows uses 6000.6.5f1/HDRP 17.6; its local bridge is editor-only.

Faustus `unity` discovery starts no editor. With the editor already open,
`action:operation` accepts `operation` and `arguments`; call `schema` for current
argument help. Read `scene`, then `object` with the exact returned object_id or
global_id. Duplicate hierarchy paths are ambiguous. Entity IDs are 64-bit text
tokens in Unity 6000.6; do not cast to an int or JavaScript number. Global IDs
become useful after saving the scene; editor object IDs expire with their domain.

Read serialized component fields before `set_properties`; pass full component
type and component_id when repeated. Unity's serialized apply retains Undo and
prefab overrides. Material shader properties use their actual installed names
(for example HDRP/Lit `_BaseColor`); inspect `material` rather than guessing.
Save prefabs/scenes explicitly and reopen to check persistence and references.
Undo restores supported in-memory edits; saving a new asset is a disk operation
and must also be verified on disk.

Use `run_tests(mode:EditMode|PlayMode)` and poll its hex test job ID with
`tests_job`. These are distinct from pending bridge numeric job IDs (`action:job`).
Pass/fail counts must come from Test Runner results. Query diagnostics for
compile errors before diagnosing a PlayMode failure as a gameplay bug.
During Play mode, scene changes may be transient: edit the source scene/prefab
when persistence is required.
