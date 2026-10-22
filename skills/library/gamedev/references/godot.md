# Godot

Read project.godot features and query the installed editor version before using
version-sensitive GDScript or physics APIs. Reuse the project's input actions,
autoloads and resource paths. Godot 3 and 4 scene and script APIs differ.

Nodes created for a saved PackedScene require the intended scene owner. A node
visible during execution can disappear when the scene is saved if ownership is
missing. Preserve imported resources and stable references when replacing a mesh
or animation. Use res:// paths within the project and project settings for units,
renderer and input; avoid machine-specific absolute asset paths.

After structural changes, parse/import with the installed Godot executable in
headless editor mode, then run the intended scene and inspect debug output.
Headless validation does not verify the camera, input feel or appearance: check
the actual viewport as well. Faustus's native `godot` operation can create,
inspect and edit saved `.tscn` scenes with Godot4.4+ using the installed engine,
query ClassDB properties, attach existing scripts, and run/read/stop a project's
debug process. Use `schema`, configure the local executable, then query `version`.
The Python API, chat tool and MCP use the same operations; no hosted API.

Choose an explicit project and res:// scene path. Scene operations retain a
job_id: poll `job` after pending or a lost response; do not replay the write.
Nodes keep scene ownership and existing scene UIDs. Property values are checked
after reopening the saved resource; runtime-only values cannot be advertised as
saved. Use expected_sha256 from inspection when editing a known version. A
concurrent on-disk edit prevents overwrite.

These operations edit saved files, not an open editor's scene buffer or undo
history. Reload externally changed files in the editor. Loading/instantiating
resources can execute project constructors and @tool scripts, as in Godot.
Binary .scn, engine migration, visual verification and C#/.NET builds are not
covered. Inspect typed properties before changing unfamiliar resources.
