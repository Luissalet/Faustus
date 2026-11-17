"""Build or audit a Unity-compatible humanoid rig on a mesh. Runs headless:

  blender -b model.blend -P rig_humanoid.py -- build --mesh Body --out rigged.blend --report rig.json
  blender -b rigged.blend -P rig_humanoid.py -- audit --report rig.json

`build` places a 15-bone humanoid skeleton from the mesh's bounding box (a first
pass, to be adjusted to the real joints), skins it with automatic weights, limits
every vertex to four influences and normalises. `audit` measures an existing rig:
required Unity Humanoid bones, unweighted vertices, influence counts and weight
sums. Bone names use the Left/Right prefix that Unity maps without manual work.
The report is JSON on `--report` and a one-line summary on stdout.
"""
import argparse
import json
import sys

import bpy
from mathutils import Vector

REQUIRED = ["Hips", "Spine", "Head", "LeftUpperLeg", "LeftLowerLeg", "LeftFoot", "RightUpperLeg", "RightLowerLeg",
            "RightFoot", "LeftUpperArm", "LeftLowerArm", "LeftHand", "RightUpperArm", "RightLowerArm", "RightHand"]
MAX_INFLUENCES = 4


def args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("build", "audit"))
    p.add_argument("--mesh", help="mesh object name; default the only mesh")
    p.add_argument("--out", help="save the rigged scene here (build)")
    p.add_argument("--report", help="write the JSON report here")
    return p.parse_args(argv)


def pick_mesh(name):
    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if name:
        return bpy.data.objects[name]
    if len(meshes) != 1:
        raise SystemExit("pass --mesh: %d mesh objects found" % len(meshes))
    return meshes[0]


def build(mesh):
    corners = [mesh.matrix_world @ Vector(c) for c in mesh.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    height, cx, cy = hi.z - lo.z, (lo.x + hi.x) / 2, (lo.y + hi.y) / 2
    width = hi.x - lo.x

    def at(x_frac, z_frac, y_frac=0.0):
        return Vector((cx + x_frac * width, cy + y_frac * height, lo.z + z_frac * height))

    arm = bpy.data.armatures.new("Armature")
    rig = bpy.data.objects.new("Rig", arm)
    bpy.context.scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    bones = {}

    def bone(name, head, tail, parent=None):
        b = arm.edit_bones.new(name)
        b.head, b.tail = head, tail
        if parent:
            b.parent = bones[parent]
            b.use_connect = (b.parent.tail - b.head).length < 1e-5
        bones[name] = b

    bone("Hips", at(0, .52), at(0, .60))
    bone("Spine", at(0, .60), at(0, .70), "Hips")
    bone("Chest", at(0, .70), at(0, .80), "Spine")
    bone("Neck", at(0, .80), at(0, .86), "Chest")
    bone("Head", at(0, .86), at(0, 1.0), "Neck")
    for side, sign in (("Left", 1), ("Right", -1)):
        shoulder = .17 * sign
        bone(side + "UpperArm", at(shoulder, .78), at(sign * .30, .78), "Chest")
        bone(side + "LowerArm", at(sign * .30, .78), at(sign * .42, .78), side + "UpperArm")
        bone(side + "Hand", at(sign * .42, .78), at(sign * .50, .78), side + "LowerArm")
        hip = .07 * sign
        bone(side + "UpperLeg", at(hip, .52), at(hip, .28), "Hips")
        bone(side + "LowerLeg", at(hip, .28), at(hip, .05), side + "UpperLeg")
        bone(side + "Foot", at(hip, .05), at(hip, .0, -.08), side + "LowerLeg")
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    mesh.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    bpy.context.view_layer.objects.active = mesh
    bpy.ops.object.mode_set(mode="OBJECT")
    mesh.select_set(True)
    bpy.ops.object.mode_set(mode="WEIGHT_PAINT")
    bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=MAX_INFLUENCES)
    bpy.ops.object.vertex_group_normalize_all(group_select_mode="ALL", lock_active=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


def audit(mesh):
    rig = next((m.object for m in mesh.modifiers if m.type == "ARMATURE" and m.object), None)
    if rig is None:
        return {"ok": False, "error": "the mesh has no Armature modifier with a rig"}
    bones = {b.name for b in rig.data.bones}
    groups = {g.index: g.name for g in mesh.vertex_groups}
    deform = {n for n in groups.values() if n in bones}
    unweighted = over = 0
    worst_sum = 0.0
    for v in mesh.data.vertices:
        ws = [g.weight for g in v.groups if groups.get(g.group) in deform and g.weight > 1e-6]
        if not ws:
            unweighted += 1
            continue
        over += len(ws) > MAX_INFLUENCES
        worst_sum = max(worst_sum, abs(sum(ws) - 1.0))
    missing = [n for n in REQUIRED if n not in bones]
    return {"ok": not missing and unweighted == 0 and over == 0 and worst_sum < 1e-3,
            "rig": rig.name, "bones": sorted(bones), "missing_required": missing, "vertices": len(mesh.data.vertices),
            "unweighted_vertices": unweighted, "over_influence_limit": over, "max_weight_sum_error": round(worst_sum, 6)}


def main():
    a = args()
    mesh = pick_mesh(a.mesh)
    if a.command == "build":
        build(mesh)
    report = audit(mesh)
    if a.command == "build" and a.out:
        bpy.ops.wm.save_as_mainfile(filepath=a.out)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)
    print("RIG", "ok" if report["ok"] else "FAIL", json.dumps({k: v for k, v in report.items() if k != "bones"}))
    if not report["ok"]:
        sys.exit(1)


main()
