# Local scenes and project debugging

`godot` is one native operation for chat, HTTP and MCP. It uses the configured
installed Godot4.4+ engine, its ClassDB, PackedScene and ResourceSaver. It does
not call a third-party MCP server or hosted API. Schema discovery starts no
editor, project, model or service. Blood Arrows continues to use Unity.

Configure the executable explicitly with `action:configure, executable:<path>`,
which updates the existing settings store, or set `FAUSTUS_GODOT`. PATH lookup
supports godot/godot4. Query version before editing version-sensitive features.
The local engine is installed separately; runtime never downloads it.

HTTP GET `/api/godot/schema`, POST `/api/godot/execute`; the POST returns the
normal tool envelope with structuredContent and exit_code. The built-in MCP
server proxies the same authenticated local API, so debug handles stay with
Faustus when the MCP client disconnects. It starts no app if Faustus is stopped.

```json
{"action":"operation","project":"D:/Games/MyGame",
 "job_id":"a7c3fa904e814537b1c9f1356df83112",
 "operation":"create_scene",
 "arguments":{"scene":"res://Stage.tscn","class":"Node2D","name":"Stage"}}
```

For `add_node`, choose scene, relative parent `node`, class, name and properties.
`set_properties` selects the existing node. Scalars and explicit Vector2,
Vector3, Vector4, Color, NodePath, StringName and project Resource values are
supported; query class_properties first. Vector/color example:
`{"$type":"Vector2","value":[12,34]}`. `inspect_scene` returns saved hierarchy,
UID and SHA256; optional fields selects properties. Attach an existing project
script with attach_script; script text is not rewritten.

New scenes require a new path. Edits stage a native resource, preserve its UID,
reopen it to verify requested properties and atomically replace the intended
file only if its prior SHA256 still matches. expected_sha256 can bind a request
to a previously inspected version. A failed batch or concurrent disk edit keeps
the current file. Native scene ownership is retained for added children.

Each scene operation has a retained job_id. Supply it before dispatch to recover
an uncertain HTTP write; an existing ID reads the original receipt even if the
new request differs. Poll `action:job` on pending. Lost dispatch receipts remain
uncertain and are never resent. Retained staged output permits recovery after
the engine completed but the server restarted before commit. The result and
commit digest are kept separately from a process's bounded debug-output tail.

`run` starts only the requested project/scene, headless by default. `frames`
optionally asks the engine to quit after a chosen frame count; zero leaves it
running. `debug` reads the exact returned process handle/cursor and `stop`
terminates only its owned process. The existing process-manager policies and
retention apply. An optional existing conversation session_id keeps its native
scope and requires normal owner verification at the HTTP API. Transport success
and exit0 do not certify absence of GDScript errors; inspect the actual output.

Operations edit disk resources, not a live editor buffer or its undo history.
Reload externally changed files in an open editor. Resource constructors and
@tool scripts may execute while scenes instantiate, including the verification
load. No guarantee is made for arbitrary project side effects. Binary .scn,
Godot3 migration, C#/.NET builds and render/input/feel verification are outside
this surface. Godot3 projects are rejected rather than migrated implicitly.

Verified engine: official4.7.2-stable on Windows, headless CPU workflows. The
official archive and measured download digest are recorded in research evidence;
this is not a claim of testing every4.x version. Official APIs:
[command line](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html),
[PackedScene](https://docs.godotengine.org/en/stable/classes/class_packedscene.html).
