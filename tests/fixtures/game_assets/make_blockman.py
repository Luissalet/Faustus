"""Blender script: a joined, closed blockman mesh 1.8 m tall, saved to the path after `--`."""
import sys

import bpy

out = sys.argv[sys.argv.index("--") + 1]
bpy.ops.wm.read_factory_settings(use_empty=True)
parts = []


def box(name, loc, scale):
    bpy.ops.mesh.primitive_cube_add(location=loc)
    o = bpy.context.active_object
    o.scale = scale
    o.name = name
    bpy.ops.object.transform_apply(scale=True)
    parts.append(o)


box("torso", (0, 0, 1.15), (.22, .12, .30))
box("head", (0, 0, 1.65), (.11, .11, .13))
box("neck", (0, 0, 1.46), (.05, .05, .06))
for sign in (1, -1):
    box("arm%d" % sign, (sign * .45, 0, 1.38), (.22, .05, .05))
    box("leg%d" % sign, (sign * .12, 0, .45), (.08, .08, .45))
bpy.ops.object.select_all(action="DESELECT")
for o in parts:
    o.select_set(True)
bpy.context.view_layer.objects.active = parts[0]
bpy.ops.object.join()
body = bpy.context.active_object
body.name = "Body"
bpy.ops.wm.save_as_mainfile(filepath=out)
