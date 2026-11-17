"""Export a Blender mesh to a Unity-ready FBX with a LOD chain, then re-import it to prove what shipped.

  blender -b asset.blend -P export_unity.py -- --object Sculpt_low --out Armor.fbx --lods 1,0.5,0.25 --report export.json

Creates `<Name>_LOD0`, `_LOD1`, ... (Unity builds a LODGroup from that suffix on import; LOD0 is the full
mesh, each next one a collapse decimate to the given ratio of LOD0's triangles). Transforms are applied,
the export uses metres, Y up, -Z forward (Unity's frame), face-smoothing groups, tangent space and only the
listed meshes. The FBX is then imported into an empty scene and measured: object names, triangle counts per
LOD (must strictly decrease), height in metres, material slots and the vertex-group (bone) count. The report
fails when LODs do not decrease, names miss the suffix, or the height drifts more than 1% from the source.
Textures are not embedded: Unity picks them up beside the FBX by material name.
"""
import argparse
import json
import os
import sys

import bpy


def parse():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--object", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--lods", default="1,0.5,0.25", help="comma-separated triangle ratios, first must be 1")
    p.add_argument("--report")
    return p.parse_args(argv)


def tri_count(obj):
    return sum(len(p.vertices) - 2 for p in obj.data.polygons)


def height(objs):
    zs = [(o.matrix_world @ v.co).z for o in objs for v in o.data.vertices]
    return max(zs) - min(zs)


def main():
    a = parse()
    ratios = [float(x) for x in a.lods.split(",")]
    if ratios[0] != 1.0:
        raise SystemExit("--lods must start with 1 (LOD0 is the full mesh)")
    src = bpy.data.objects[a.object]
    base = src.name.replace("_low", "")
    made = []
    for i, ratio in enumerate(ratios):
        lod = src.copy()
        lod.data = src.data.copy()
        lod.name = "%s_LOD%d" % (base, i)
        lod.data.name = lod.name
        bpy.context.scene.collection.objects.link(lod)
        bpy.ops.object.select_all(action="DESELECT")
        lod.select_set(True)
        bpy.context.view_layer.objects.active = lod
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        if ratio < 1:
            mod = lod.modifiers.new("Decimate", "DECIMATE")
            mod.ratio = ratio
            bpy.ops.object.modifier_apply(modifier=mod.name)
        made.append(lod)
    expected_height = height([made[0]])
    bpy.ops.object.select_all(action="DESELECT")
    for lod in made:
        lod.select_set(True)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    bpy.ops.export_scene.fbx(filepath=a.out, use_selection=True, object_types={"MESH"}, apply_unit_scale=True,
                             global_scale=1.0, apply_scale_options="FBX_SCALE_ALL", axis_forward="-Z", axis_up="Y",
                             bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True, add_leaf_bones=False,
                             path_mode="STRIP", use_mesh_modifiers=True)
    # Re-import in a clean scene and measure the file, not the source.
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=a.out)
    got = sorted((o for o in bpy.context.scene.objects if o.type == "MESH"), key=lambda o: o.name)
    lods = [{"name": o.name, "triangles": tri_count(o), "materials": len(o.material_slots), "vertex_groups": len(o.vertex_groups)} for o in got]
    tris = [l["triangles"] for l in lods]
    imported_height = height(got[:1]) if got else 0
    problems = []
    if len(got) != len(ratios):
        problems.append("expected %d meshes in the FBX, found %d" % (len(ratios), len(got)))
    if any("_LOD" not in l["name"] for l in lods):
        problems.append("a mesh name lacks the _LOD suffix")
    if any(b >= a_ for a_, b in zip(tris, tris[1:])):
        problems.append("triangle counts do not strictly decrease: %s" % tris)
    # The FBX is Y up; Blender's importer turns it back to Z up, so height is comparable with the source.
    if expected_height and abs(imported_height - expected_height) / expected_height > 0.01:
        problems.append("height changed from %.3f to %.3f m" % (expected_height, imported_height))
    report = {"fbx": os.path.abspath(a.out), "bytes": os.path.getsize(a.out), "height_m": round(imported_height, 4), "lods": lods,
              "problems": problems, "ok": not problems}
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), indent=1)
    print("EXPORT", "ok" if report["ok"] else "FAIL", json.dumps(report))
    if not report["ok"]:
        sys.exit(1)


main()
