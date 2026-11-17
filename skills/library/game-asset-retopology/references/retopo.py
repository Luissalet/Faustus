"""Produce a clean low-poly mesh from a high-poly one and measure how faithful it is.

  blender -b high.blend -P retopo.py -- --source Sculpt --faces 2000 --out low.blend --report retopo.json

Method `quadriflow` (default) builds an all-quad layout with Quadriflow; if the source is not manifold or
Quadriflow fails, it falls back to `voxel+decimate` (a voxel remesh to close and clean the shell, then a
collapse decimate to the target). The low mesh is a new object named `<source>_low`; the source is left untouched and
renderable (a bake needs the high mesh visible to the renderer). The report gives face/triangle counts, quad share, non-manifold edges and the
deviation of the low mesh from the source (mean and max, as a fraction of the bounding-box diagonal,
both directions). Fail thresholds: --max-deviation (largest, default 0.06) and --max-mean-deviation
(default 0.01). Thin parts such as ears set the maximum; read where it is before raising the limit.
"""
import argparse
import json
import sys

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree


def parse():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--faces", type=int, default=2000, help="target face count (quads for quadriflow)")
    p.add_argument("--method", choices=("quadriflow", "voxel+decimate"), default="quadriflow")
    p.add_argument("--max-deviation", type=float, default=0.06)
    p.add_argument("--max-mean-deviation", type=float, default=0.01)
    p.add_argument("--out")
    p.add_argument("--report")
    return p.parse_args(argv)


def select_only(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def duplicate(src, name):
    new = src.copy()
    new.data = src.data.copy()
    new.name = name
    bpy.context.scene.collection.objects.link(new)
    return new


def quadriflow(low, faces):
    select_only(low)
    bpy.ops.object.quadriflow_remesh(target_faces=faces, use_mesh_symmetry=False, use_preserve_sharp=False, smooth_normals=True)


def voxel_decimate(low, faces, diagonal):
    select_only(low)
    low.data.remesh_voxel_size = diagonal / 160
    bpy.ops.object.voxel_remesh()
    tris = sum(len(p.vertices) - 2 for p in low.data.polygons)
    mod = low.modifiers.new("Decimate", "DECIMATE")
    mod.ratio = min(1.0, faces * 2 / max(tris, 1))
    bpy.ops.object.modifier_apply(modifier=mod.name)


def surface_points(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = obj.evaluated_get(depsgraph)
    mesh = ev.to_mesh()
    pts = [obj.matrix_world @ v.co for v in mesh.vertices]
    tree = BVHTree.FromObject(obj, depsgraph)
    ev.to_mesh_clear()
    return pts, tree


def deviation(a, b):
    pts, _ = surface_points(a)
    _, tree = surface_points(b)
    dist = [(tree.find_nearest(p)[3]) for p in pts]
    dist = [d for d in dist if d is not None]
    return sum(dist) / len(dist), max(dist)


def stats(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    quads = sum(1 for f in bm.faces if len(f.verts) == 4)
    result = {"faces": len(bm.faces), "triangles": sum(len(f.verts) - 2 for f in bm.faces),
              "quad_share": round(quads / max(len(bm.faces), 1), 3),
              "non_manifold_edges": sum(1 for e in bm.edges if not e.is_manifold)}
    bm.free()
    return result


def main():
    a = parse()
    src = bpy.data.objects[a.source]
    corners = [src.matrix_world @ Vector(c) for c in src.bound_box]
    diagonal = (Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners))) -
                Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))).length
    low = duplicate(src, src.name + "_low")
    used = a.method
    if a.method == "quadriflow":
        try:
            quadriflow(low, a.faces)
        except RuntimeError as e:
            used = "voxel+decimate"
            print("quadriflow failed, falling back:", str(e).strip().splitlines()[-1])
            bpy.data.objects.remove(low)
            low = duplicate(src, src.name + "_low")
    if used == "voxel+decimate":
        voxel_decimate(low, a.faces, diagonal)
    mean_a, max_a = deviation(low, src)
    mean_b, max_b = deviation(src, low)
    low_stats = stats(low)
    report = {"method": used, "source": {"name": src.name, **stats(src)}, "low": {"name": low.name, **low_stats},
              "deviation": {"mean": round(max(mean_a, mean_b) / diagonal, 5), "max": round(max(max_a, max_b) / diagonal, 5)}}
    report["ok"] = (report["deviation"]["max"] <= a.max_deviation and report["deviation"]["mean"] <= a.max_mean_deviation
                    and low_stats["faces"] > 0)
    if a.out:
        bpy.ops.wm.save_as_mainfile(filepath=a.out)
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), indent=1)
    print("RETOPO", "ok" if report["ok"] else "FAIL", json.dumps(report))
    if not report["ok"]:
        sys.exit(1)


main()
