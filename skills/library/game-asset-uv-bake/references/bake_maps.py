"""Unwrap a low-poly mesh and bake tangent-space normal and ambient-occlusion maps from a high-poly one.

  blender -b low.blend -P bake_maps.py -- --high Sculpt --low Sculpt_low --size 1024 --outdir maps --report bake.json

Unwraps the low mesh with Smart UV Project when it has no UV map (an existing layout is kept), bakes
with Cycles on the CPU (no GPU is touched), and writes `<low>_normal.png` (OpenGL convention, Y up: what
Unity and glTF expect) and `<low>_ao.png` into --outdir. The report measures UV coverage, overlap,
islands outside 0-1 and, per map, whether it is flat or carries signal (standard deviation and fraction
of pixels differing from the background), so an empty bake fails loudly instead of shipping.
"""
import argparse
import json
import math
import os
import sys

import bmesh
import bpy


def parse():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--high", required=True)
    p.add_argument("--low", required=True)
    p.add_argument("--size", type=int, default=1024)
    p.add_argument("--samples", type=int, default=16)
    p.add_argument("--margin", type=int, default=8, help="pixel padding around islands")
    p.add_argument("--cage", type=float, default=0.03, help="ray start distance above the low surface, as a fraction of the bounding-box diagonal")
    p.add_argument("--outdir", required=True)
    p.add_argument("--report")
    return p.parse_args(argv)


def uv_report(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    layer = bm.loops.layers.uv.active
    if layer is None:
        bm.free()
        return {"has_uv": False}
    area = outside = 0.0
    cells, size = {}, 256
    overlap = 0
    for f in bm.faces:
        uvs = [l[layer].uv for l in f.loops]
        a = abs(sum(uvs[i].x * uvs[(i + 1) % len(uvs)].y - uvs[(i + 1) % len(uvs)].x * uvs[i].y for i in range(len(uvs)))) / 2
        area += a
        cx, cy = sum(u.x for u in uvs) / len(uvs), sum(u.y for u in uvs) / len(uvs)
        if not (0 <= cx <= 1 and 0 <= cy <= 1):
            outside += 1
        key = (int(cx * size), int(cy * size))
        if key in cells and cells[key] != f.index:
            overlap += 1
        cells[key] = f.index
    n = len(bm.faces)
    bm.free()
    return {"has_uv": True, "coverage": round(area, 4), "faces_outside_0_1": int(outside), "face_centres_sharing_a_cell": overlap, "faces": n}


def unwrap(low):
    if low.data.uv_layers:
        return "kept"
    bpy.ops.object.select_all(action="DESELECT")
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.003)
    bpy.ops.object.mode_set(mode="OBJECT")
    return "smart_project"


def image_stats(path):
    img = bpy.data.images.load(path)
    px = list(img.pixels)
    n = len(px) // 4
    mean = [sum(px[c::4]) / n for c in range(3)]
    var = sum(sum((px[i * 4 + c] - mean[c]) ** 2 for c in range(3)) for i in range(0, n, 4)) / (n / 4 * 3)
    edge = [px[c] for c in range(3)]
    differing = sum(1 for i in range(0, n, 4) if max(abs(px[i * 4 + c] - edge[c]) for c in range(3)) > 0.02) / (n / 4)
    bpy.data.images.remove(img)
    return {"std": round(math.sqrt(var), 4), "signal_fraction": round(differing, 3)}


def bake(low, high, kind, a, path):
    mat = low.active_material or bpy.data.materials.new(low.name + "_mat")
    mat.use_nodes = True
    if not low.data.materials:
        low.data.materials.append(mat)
    nodes = mat.node_tree.nodes
    tex = nodes.new("ShaderNodeTexImage")
    image = bpy.data.images.new(low.name + "_" + kind, a.size, a.size, float_buffer=False)
    image.colorspace_settings.name = "Non-Color"
    tex.image = image
    nodes.active = tex
    bpy.ops.object.select_all(action="DESELECT")
    high.select_set(True)
    low.select_set(True)
    bpy.context.view_layer.objects.active = low
    diag = max(low.dimensions) * 1.7
    bpy.ops.object.bake(type=kind.upper(), use_selected_to_active=True, cage_extrusion=a.cage * diag, max_ray_distance=a.cage * diag * 3,
                        margin=a.margin, margin_type="EXTEND", normal_space="TANGENT",
                        normal_r="POS_X", normal_g="POS_Y", normal_b="POS_Z", use_clear=True)
    image.filepath_raw = path
    image.file_format = "PNG"
    image.save()
    nodes.remove(tex)
    return image_stats(path)


def main():
    a = parse()
    high, low = bpy.data.objects[a.high], bpy.data.objects[a.low]
    os.makedirs(a.outdir, exist_ok=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = a.samples
    scene.cycles.use_denoise = False
    if scene.world is None:
        scene.world = bpy.data.worlds.new("BakeWorld")
    scene.world.light_settings.distance = max(low.dimensions) * 0.25  # occlusion reach, scene units
    unwrapped = unwrap(low)
    report = {"unwrap": unwrapped, "uv": uv_report(low), "maps": {}}
    for kind in ("normal", "ao"):
        report["maps"][kind] = bake(low, high, kind, a, os.path.join(a.outdir, "%s_%s.png" % (low.name, kind)))
    report["ok"] = (report["uv"].get("has_uv") and report["uv"]["faces_outside_0_1"] == 0 and
                    all(m["signal_fraction"] > 0.05 for m in report["maps"].values()) and report["maps"]["ao"]["std"] > 0.01)
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), indent=1)
    print("BAKE", "ok" if report["ok"] else "FAIL", json.dumps(report))
    if not report["ok"]:
        sys.exit(1)


main()
