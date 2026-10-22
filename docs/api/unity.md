# Local Unity editor

The native `unity` tool is shared by chat, `POST /api/unity/execute` and the
built-in Unity MCP server (`mcp_servers/unity_server.py`). Discovery starts no
editor, model or Hoard. `GET /api/unity/schema` returns operation help.

The Unity project must contain the editor bridge shipped in Blood Arrows under
`Assets/BloodArrows/Tools/Bridge/Editor`. Open that project in its installed
Unity editor. The bridge writes its loopback port to `Logs/bridge_port.txt`.
Faustus checks the returned project identity before sending an edit. It never
replays an edit after a timeout: inspect the result or current scene first.

```json
{"action":"operation","project":"D:/Games/Example","operation":"scene"}
```

Read `scene`, then `object` using an exact `object_id`, saved `global_id` or
unique hierarchy path. Unity 6000.6 entity identifiers are returned as prefixed
strings to retain all 64 bits. Earlier editors use prefixed instance identifiers.
Editor IDs expire with their domain; saved scene GlobalObjectIds are preferable
for persistence. Repeated components require the returned `component_id`.

Available operations cover serialized component fields; object and component
creation; selection; installed shader material inspection, creation, editing
and assignment; prefab creation/instantiation; additive scene creation/loading,
explicit saving and unloading; Undo/Redo; Play/Pause/Stop/Step; and Test Runner.
Unsupported serialized field types return an error; supported child properties
can still be edited. Material texture assignment is not yet included. New
material/prefab paths must not overwrite existing assets. Saving an existing
scene is explicit. Unsaved open scenes cannot be silently discarded by loading
or unloading. Undo is Unity's editor history, not a disk backup.

```json
{"action":"operation","project":"D:/Games/Example","operation":"set_properties","arguments":{"object":"entity:123456789","component":"UnityEngine.Transform","properties":{"m_LocalPosition":[1,2,3]}}}
```

An HTTP bridge operation can return `state:pending` and a numeric `job_id`.
Use `{"action":"job","project":"…","job_id":7}`; do not resubmit the edit.
If identity inspection itself is pending, `requested_operation_sent:false`
means the intended operation has not been submitted. Poll that inspection,
then submit the intended operation once identity is confirmed. Pending bridge
jobs are in memory and may be lost with a domain reload; read the scene before
deciding to repeat an uncertain edit.

`run_tests` accepts `mode:EditMode|PlayMode` and an optional full-name `filter`.
Its hex test job identifier is polled with the `tests_job` operation, not
`action:job`. Actual leaf results, counts and XML remain in
`Logs/faustus-tests` after the run and editor reloads. A filter matching zero
cases returns `state:no_tests`, rather than a successful test claim. A `running` file does
not certify a live runner after a crash; completion needs recorded callbacks.
`diagnostics` returns the existing bridge compile state and indexed console.

`project_profile` reads engine versions and package metadata in Unity, Godot,
Unreal or Three.js project folders without an editor or network access. Declared
package ranges, Godot features and Unreal engine associations are labelled as
such. This metadata reader does not provide native Godot/Unreal editor control.
The bundled `gamedev` skill routes to the appropriate engine reference and is
also readable as a standard SKILL.md by other coding agents.

These operations extend the project's existing native bridge. Workflow sources:
[MCP Unity](https://github.com/CoderGamester/mcp-unity/tree/d572dc88f1b982a0fdfc09cd14cfdfd760b658e8)
(MIT) and [game development skills](https://github.com/gamedev-skills/awesome-gamedev-agent-skills/tree/38a4c2500eeb5ff9ac084336b8f66108069d2f8d)
(Apache-2.0). Their transports and hosted services are not runtime dependencies.
