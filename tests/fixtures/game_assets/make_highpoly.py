"""Blender script: a dense Suzanne ("Sculpt", ~60k faces) saved to the path after `--`."""
import sys

import bpy

out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_monkey_add(size=2)
obj = bpy.context.active_object
obj.name = "Sculpt"
mod = obj.modifiers.new("Subsurf", "SUBSURF")
mod.levels = 4
bpy.ops.object.modifier_apply(modifier=mod.name)
bpy.ops.object.shade_smooth()
bpy.ops.wm.save_as_mainfile(filepath=out)
