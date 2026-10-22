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
the actual viewport as well. Native live editor automation is only available
when an appropriate local bridge is installed; metadata discovery does not
provide one.
