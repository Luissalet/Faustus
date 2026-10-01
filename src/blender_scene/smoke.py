"""A small scene that exercises the whole path: build, render, save, verify."""
from __future__ import annotations

from typing import Dict


def smoke_scene(engine: str = "workbench", name: str = "smoke") -> Dict:
    """Ground, a beveled cube with a material, a sphere, a sun, a camera that
    tracks the cube, a 320x240 low-sample render, a .blend and assertions.

    ``engine`` is ``workbench`` (fast, works without a GPU), ``eevee`` or
    ``cycles``. Outputs go to ``${PROJECT_ROOT}/<name>.png`` and ``.blend``.
    """
    png = "${PROJECT_ROOT}/%s.png" % name
    blend = "${PROJECT_ROOT}/%s.blend" % name
    return {
        "meta": {"name": "smoke scene", "description": "Cube, sphere and ground rendered small, saved and checked."},
        "ops": [
            {"op": "new_scene"},
            {"op": "set_world", "color": [0.05, 0.07, 0.1], "strength": 1.0},
            {"op": "add_material", "name": "SmokeBlue", "base_color": "#2a7fff", "metallic": 0.2, "roughness": 0.35},
            {"op": "add_mesh", "primitive": "plane", "name": "Ground", "size": 10},
            {"op": "add_mesh", "primitive": "cube", "name": "SmokeCube", "location": [0, 0, 1], "material": "SmokeBlue"},
            {"op": "add_modifier", "target": "SmokeCube", "type": "bevel", "width": 0.08, "bevel_segments": 3},
            {"op": "add_mesh", "primitive": "uv_sphere", "name": "SmokeSphere", "location": [2.2, 0.5, 0.6],
             "radius": 0.6, "smooth": True},
            {"op": "add_light", "type": "sun", "name": "SmokeSun", "rotation": [50, 0, 30], "energy": 3},
            {"op": "add_camera", "name": "SmokeCamera", "location": [6, -6, 4], "lens": 40},
            {"op": "track_to", "object": "SmokeCamera", "target": "SmokeCube"},
            {"op": "set_render", "engine": engine, "resolution": [320, 240], "percentage": 100, "samples": 8},
            {"op": "render", "path": png},
            {"op": "save", "path": blend},
        ] + verification_ops(png, blend),
    }


def verification_ops(png: str, blend: str) -> list:
    """Assertions that hold for the smoke scene, live or re-loaded from its .blend."""
    return [
        {"op": "assert", "kind": "object_count", "object_type": "mesh", "count": 3},
        {"op": "assert", "kind": "object_count", "count": 5},
        {"op": "assert", "kind": "object_exists", "name": "SmokeCube"},
        {"op": "assert", "kind": "material_assigned", "object": "SmokeCube", "material": "SmokeBlue"},
        {"op": "assert", "kind": "resolution", "width": 320, "height": 240},
        {"op": "assert", "kind": "camera_active", "name": "SmokeCamera"},
        {"op": "assert", "kind": "bbox_within", "object": "SmokeCube",
         "min": [-1.2, -1.2, -0.2], "max": [1.2, 1.2, 2.2]},
        {"op": "assert", "kind": "file_exists", "path": png, "min_bytes": 500},
        {"op": "assert", "kind": "file_exists", "path": blend, "min_bytes": 1000},
    ]


def reload_scene(name: str = "smoke", engine: str = "workbench") -> Dict:
    """Re-open the saved smoke .blend in a fresh Blender and assert again."""
    png = "${PROJECT_ROOT}/%s.png" % name
    blend = "${PROJECT_ROOT}/%s.blend" % name
    return {
        "meta": {"name": "smoke scene reload", "description": "Verify the saved .blend by loading it."},
        "ops": [{"op": "load_blend", "path": blend},
                {"op": "assert", "kind": "engine", "engine": engine}] + verification_ops(png, blend),
    }
