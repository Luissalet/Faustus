"""Runs a normalised scene inside Blender.

    blender --background --factory-startup --python blender_runner.py -- <scene.json> <root>

Executed by Blender's own Python, not by Faustus: it imports ``bpy`` and the
standard library only (plus ``scene_common`` from its own folder). It applies
the ops in order. An op that fails is recorded and the run continues unless
``meta.stop_on_error`` is set. Exactly one line ``BLENDER_SCENE_REPORT {json}``
is printed at the end; the process exits 0 only when nothing failed.

There is no op that evaluates code. Every path is confined to ``<root>`` here
again, whatever the validator did.
"""
import fnmatch
import json
import math
import os
import sys
import time
import traceback

import bpy  # noqa: E402  (only importable inside Blender)

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import scene_common as C  # noqa: E402

VERSION = tuple(bpy.app.version)
OBJECT_TYPES = {
    "mesh": "MESH", "light": "LIGHT", "camera": "CAMERA", "empty": "EMPTY",
    "text": "FONT", "curve": "CURVE", "armature": "ARMATURE", "surface": "SURFACE",
    "lattice": "LATTICE", "grease_pencil": "GPENCIL",
}


class OpError(Exception):
    """A failure with a message meant for the author of the scene."""


class State:
    root = ""
    outputs = []
    warnings = []
    assertions = []
    current_index = 0


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def rad(vec):
    return [math.radians(float(v)) for v in vec]


def scene():
    return bpy.context.scene


def path_of(value, what="path"):
    resolved, err = C.confine_path(value, State.root)
    if err:
        raise OpError(f"{what}: {err}")
    return resolved


def get_object(name, what="object"):
    ob = bpy.data.objects.get(name)
    if ob is None:
        known = ", ".join(sorted(o.name for o in bpy.data.objects)[:25]) or "none"
        raise OpError(f"{what} '{name}' not found (objects: {known})")
    return ob


def get_material(name):
    mat = bpy.data.materials.get(name)
    if mat is None:
        known = ", ".join(sorted(m.name for m in bpy.data.materials)[:25]) or "none"
        raise OpError(f"material '{name}' not found; create it with add_material first (materials: {known})")
    return mat


def check_free(collection, name, kind):
    if name and name in collection:
        raise OpError(f"{kind} '{name}' already exists; choose another name or delete it first")


def link(ob):
    scene().collection.objects.link(ob)
    return ob


def apply_transform(ob, op):
    if "location" in op:
        ob.location = op["location"]
    if "rotation" in op:
        ob.rotation_euler = rad(op["rotation"])
    if "scale" in op:
        ob.scale = op["scale"]


def pick_input(node, names, value):
    """Set the first socket of ``names`` that exists (sockets were renamed across versions)."""
    for n in names:
        sock = node.inputs.get(n)
        if sock is not None:
            sock.default_value = value
            return n
    return None


def selected_only(objects):
    for o in bpy.context.view_layer.objects:
        try:
            o.select_set(False)
        except Exception:
            pass
    first = None
    for o in objects:
        o.select_set(True)
        first = first or o
    if first is not None:
        bpy.context.view_layer.objects.active = first


def has_op(path):
    mod, _, name = path.partition(".")
    return hasattr(getattr(bpy.ops, mod, None), name)


def record_output(path, kind):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    State.outputs.append({"path": path, "bytes": size, "kind": kind})
    return size


def reset_empty():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        bpy.context.preferences.filepaths.save_version = 0   # no .blend1 backups
    except Exception:
        pass


# --------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------

def op_new_scene(op):
    reset_empty()


def op_load_blend(op):
    path = path_of(op["path"])
    if not os.path.isfile(path):
        raise OpError(f"file not found: {path}")
    bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
    try:
        bpy.context.preferences.filepaths.save_version = 0
    except Exception:
        pass
    return {"objects": len(bpy.context.scene.objects)}


def op_save(op):
    path = path_of(op["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=bool(op.get("compress", False)), copy=True)
    return {"bytes": record_output(path, "blend")}


def op_delete(op):
    types = OBJECT_TYPES.get(op.get("object_type", ""), None)
    victims = [o for o in bpy.data.objects
               if fnmatch.fnmatchcase(o.name, op["pattern"]) and (types is None or o.type == types)]
    if not victims and op.get("must_match"):
        raise OpError(f"no object matches '{op['pattern']}'")
    for o in victims:
        bpy.data.objects.remove(o, do_unlink=True)
    return {"deleted": len(victims)}


def _background_node(world):
    if not world.use_nodes:
        world.use_nodes = True
    nt = world.node_tree
    bg = nt.nodes.get("Background")
    if bg is None:
        bg = nt.nodes.new("ShaderNodeBackground")
        out = nt.nodes.get("World Output") or nt.nodes.new("ShaderNodeOutputWorld")
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
    return nt, bg


def op_set_world(op):
    sc = scene()
    world = sc.world or bpy.data.worlds.new("World")
    sc.world = world
    nt, bg = _background_node(world)
    if "color" in op:
        bg.inputs["Color"].default_value = op["color"]
    if "strength" in op:
        bg.inputs["Strength"].default_value = op["strength"]
    if "hdri" in op:
        path = path_of(op["hdri"], "hdri")
        if not os.path.isfile(path):
            raise OpError(f"hdri file not found: {path}")
        img = bpy.data.images.load(path, check_existing=True)
        env = nt.nodes.new("ShaderNodeTexEnvironment")
        env.image = img
        coord = nt.nodes.new("ShaderNodeTexCoord")
        mapping = nt.nodes.new("ShaderNodeMapping")
        mapping.inputs["Rotation"].default_value[2] = math.radians(float(op.get("hdri_rotation", 0)))
        nt.links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        nt.links.new(mapping.outputs["Vector"], env.inputs["Vector"])
        nt.links.new(env.outputs["Color"], bg.inputs["Color"])
    return {"world": world.name}


def _add_primitive(op):
    p = op["primitive"]
    if p == "cube":
        bpy.ops.mesh.primitive_cube_add(size=op.get("size", 2.0))
    elif p == "plane":
        bpy.ops.mesh.primitive_plane_add(size=op.get("size", 2.0))
    elif p == "monkey":
        bpy.ops.mesh.primitive_monkey_add(size=op.get("size", 2.0))
    elif p == "uv_sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(segments=op.get("segments", 32), ring_count=op.get("rings", 16),
                                             radius=op.get("radius", 1.0))
    elif p == "ico_sphere":
        bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=op.get("subdivisions", 2), radius=op.get("radius", 1.0))
    elif p == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(vertices=op.get("segments", 32), radius=op.get("radius", 1.0),
                                            depth=op.get("depth", 2.0))
    elif p == "cone":
        bpy.ops.mesh.primitive_cone_add(vertices=op.get("segments", 32), radius1=op.get("radius", 1.0),
                                        radius2=op.get("radius2", 0.0), depth=op.get("depth", 2.0))
    elif p == "torus":
        bpy.ops.mesh.primitive_torus_add(major_segments=op.get("major_segments", 48),
                                         minor_segments=op.get("minor_segments", 12),
                                         major_radius=op.get("major_radius", 1.0),
                                         minor_radius=op.get("minor_radius", 0.25))
    else:
        raise OpError(f"unknown primitive '{p}'")
    return bpy.context.active_object


def _assign_material(ob, mat, slot=0):
    if ob.type == "EMPTY" or getattr(ob.data, "materials", None) is None:
        raise OpError(f"object '{ob.name}' ({ob.type}) cannot hold a material")
    mats = ob.data.materials
    if len(mats) == 0:
        mats.append(mat)
    elif slot < len(mats):
        ob.material_slots[slot].material = mat
    else:
        raise OpError(f"object '{ob.name}' has {len(mats)} material slot(s); slot {slot} does not exist")


def op_add_mesh(op):
    name = op.get("name")
    check_free(bpy.data.objects, name, "object")
    mat = get_material(op["material"]) if "material" in op else None
    ob = _add_primitive(op)
    if name:
        ob.name = name
    apply_transform(ob, op)
    if op.get("smooth"):
        for poly in ob.data.polygons:
            poly.use_smooth = True
    if mat is not None:
        _assign_material(ob, mat)
    return {"object": ob.name, "vertices": len(ob.data.vertices)}


def op_set_transform(op):
    ob = get_object(op["target"], "target")
    apply_transform(ob, op)
    return {"object": ob.name}


_EMPTY_DISPLAY = {"plain_axes": "PLAIN_AXES", "arrows": "ARROWS", "single_arrow": "SINGLE_ARROW",
                  "circle": "CIRCLE", "cube": "CUBE", "sphere": "SPHERE", "cone": "CONE"}


def op_add_empty(op):
    name = op.get("name") or "Empty"
    check_free(bpy.data.objects, op.get("name"), "object")
    ob = bpy.data.objects.new(name, None)
    ob.empty_display_type = _EMPTY_DISPLAY[op.get("display", "plain_axes")]
    ob.empty_display_size = op.get("size", 1.0)
    link(ob)
    apply_transform(ob, op)
    return {"object": ob.name}


def op_add_material(op):
    check_free(bpy.data.materials, op["name"], "material")
    mat = bpy.data.materials.new(op["name"])
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        raise OpError("the new material has no Principled BSDF node")
    set_ = {}
    # The viewport/Workbench colour mirrors the node values, so a Workbench
    # render of the scene still shows the material.
    if "base_color" in op:
        set_["base_color"] = pick_input(bsdf, ("Base Color",), op["base_color"])
        mat.diffuse_color = op["base_color"]
    if "metallic" in op:
        pick_input(bsdf, ("Metallic",), op["metallic"])
        mat.metallic = op["metallic"]
    if "roughness" in op:
        pick_input(bsdf, ("Roughness",), op["roughness"])
        mat.roughness = op["roughness"]
    if "ior" in op:
        pick_input(bsdf, ("IOR",), op["ior"])
    if "transmission" in op:
        pick_input(bsdf, ("Transmission Weight", "Transmission"), op["transmission"])
    if "alpha" in op:
        pick_input(bsdf, ("Alpha",), op["alpha"])
        if op["alpha"] < 1.0:
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
            if hasattr(mat, "blend_method"):
                try:
                    mat.blend_method = "BLEND"
                except Exception:
                    pass
    if "emission_color" in op or "emission_strength" in op:
        color = op.get("emission_color", [1.0, 1.0, 1.0, 1.0])
        strength = op.get("emission_strength", 1.0)
        pick_input(bsdf, ("Emission Color", "Emission"), color)
        pick_input(bsdf, ("Emission Strength",), strength)
    return {"material": mat.name}


def op_assign_material(op):
    ob = get_object(op["target"], "target")
    _assign_material(ob, get_material(op["material"]), op.get("slot", 0))
    return {"object": ob.name, "material": op["material"]}


def op_add_modifier(op):
    ob = get_object(op["target"], "target")
    if ob.type not in ("MESH", "CURVE", "FONT", "SURFACE", "LATTICE"):
        raise OpError(f"object '{ob.name}' ({ob.type}) cannot take a {op['type']} modifier")
    kind = op["type"]
    mod_name = op.get("name") or kind.capitalize()
    if kind == "bevel":
        mod = ob.modifiers.new(mod_name, "BEVEL")
        if "width" in op:
            mod.width = op["width"]
        if "bevel_segments" in op:
            mod.segments = op["bevel_segments"]
        if "angle_limit" in op:
            mod.limit_method = "ANGLE"
            mod.angle_limit = math.radians(op["angle_limit"])
    elif kind == "subdivision":
        mod = ob.modifiers.new(mod_name, "SUBSURF")
        if "levels" in op:
            mod.levels = op["levels"]
        if "render_levels" in op:
            mod.render_levels = op["render_levels"]
    elif kind == "solidify":
        mod = ob.modifiers.new(mod_name, "SOLIDIFY")
        if "thickness" in op:
            mod.thickness = op["thickness"]
        if "offset" in op:
            mod.offset = op["offset"]
    elif kind == "array":
        mod = ob.modifiers.new(mod_name, "ARRAY")
        if "count" in op:
            mod.count = op["count"]
        if "relative_offset" in op:
            mod.use_relative_offset = True
            mod.relative_offset_displace = op["relative_offset"]
        if "constant_offset" in op:
            mod.use_constant_offset = True
            mod.constant_offset_displace = op["constant_offset"]
    elif kind == "mirror":
        mod = ob.modifiers.new(mod_name, "MIRROR")
        axes = op.get("axes", ["x"])
        mod.use_axis = [a in axes for a in ("x", "y", "z")]
        if "clip" in op:
            mod.use_clip = op["clip"]
    else:
        raise OpError(f"unknown modifier '{kind}'")
    return {"object": ob.name, "modifier": mod.name}


_LIGHT_DEFAULT_ENERGY = {"point": 1000.0, "sun": 3.0, "spot": 1000.0, "area": 500.0}


def op_add_light(op):
    kind = op["type"]
    name = op.get("name") or kind.capitalize() + "Light"
    check_free(bpy.data.objects, op.get("name"), "object")
    light = bpy.data.lights.new(name, type=kind.upper())
    light.energy = op.get("energy", _LIGHT_DEFAULT_ENERGY[kind])
    if "color" in op:
        light.color = op["color"][:3]
    if "size" in op:
        if kind == "sun":
            light.angle = math.radians(op["size"])
        elif kind == "area":
            light.size = op["size"]
        else:
            light.shadow_soft_size = op["size"]
    if kind == "spot":
        if "spot_angle" in op:
            light.spot_size = math.radians(op["spot_angle"])
        if "spot_blend" in op:
            light.spot_blend = op["spot_blend"]
    ob = link(bpy.data.objects.new(name, light))
    apply_transform(ob, op)
    return {"object": ob.name}


def op_add_camera(op):
    name = op.get("name") or "Camera"
    check_free(bpy.data.objects, op.get("name"), "object")
    cam = bpy.data.cameras.new(name)
    if "lens" in op:
        cam.lens = op["lens"]
    if "sensor_width" in op:
        cam.sensor_width = op["sensor_width"]
    if op.get("projection") == "orthographic":
        cam.type = "ORTHO"
        cam.ortho_scale = op.get("ortho_scale", 6.0)
    focus_obj = op.get("dof_focus_object")
    if focus_obj or "dof_focus_distance" in op or "f_stop" in op:
        cam.dof.use_dof = True
        if focus_obj:
            cam.dof.focus_object = get_object(focus_obj, "dof_focus_object")
        if "dof_focus_distance" in op:
            cam.dof.focus_distance = op["dof_focus_distance"]
        if "f_stop" in op:
            cam.dof.aperture_fstop = op["f_stop"]
    ob = link(bpy.data.objects.new(name, cam))
    apply_transform(ob, op)
    if op.get("make_active", True):
        scene().camera = ob
    return {"object": ob.name, "active": scene().camera is ob}


def op_track_to(op):
    ob = get_object(op["object"], "object")
    target = get_object(op["target"], "target")
    if ob is target:
        raise OpError("an object cannot track itself")
    con = ob.constraints.new("TRACK_TO")
    con.target = target
    con.track_axis = "TRACK_NEGATIVE_Z"
    con.up_axis = "UP_Y"
    return {"object": ob.name, "target": target.name}


def op_add_text(op):
    name = op.get("name") or "Text"
    check_free(bpy.data.objects, op.get("name"), "object")
    mat = get_material(op["material"]) if "material" in op else None
    curve = bpy.data.curves.new(name, "FONT")
    curve.body = op["body"]
    curve.size = op.get("size", 1.0)
    curve.extrude = op.get("extrude", 0.0)
    ob = link(bpy.data.objects.new(name, curve))
    apply_transform(ob, op)
    if mat is not None:
        _assign_material(ob, mat)
    return {"object": ob.name}


_VIEW_TRANSFORMS = {"standard": ("Standard",), "filmic": ("Filmic", "AgX", "Standard"),
                    "agx": ("AgX", "Filmic", "Standard"), "raw": ("Raw",),
                    "neutral": ("Khronos PBR Neutral", "AgX", "Standard")}
_WORKBENCH_AA = ["OFF", "FXAA", "5", "8", "11", "16", "32"]


def _enable_gpu():
    """Turn on a usable Cycles compute device; return its type or None."""
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
    except Exception:
        return None
    for kind in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
        try:
            prefs.compute_device_type = kind
            prefs.get_devices()
            devices = [d for d in prefs.devices if d.type == kind]
        except Exception:
            continue
        if devices:
            for d in devices:
                d.use = True
            return kind
    try:
        prefs.compute_device_type = "NONE"
    except Exception:
        pass
    return None


def op_set_render(op):
    sc = scene()
    info = {}
    if "engine" in op:
        sc.render.engine = C.engine_name(op["engine"], VERSION)
        info["engine"] = sc.render.engine
        if op["engine"] == "workbench":
            try:
                sc.display.shading.light = "STUDIO"
                sc.display.shading.color_type = "MATERIAL"
            except Exception:
                pass
    if "resolution" in op:
        sc.render.resolution_x, sc.render.resolution_y = op["resolution"]
    if "percentage" in op:
        sc.render.resolution_percentage = op["percentage"]
    if "samples" in op:
        logical = C.logical_engine(sc.render.engine)
        if logical == "cycles":
            sc.cycles.samples = op["samples"]
            sc.cycles.preview_samples = min(op["samples"], 32)
        elif logical == "eevee":
            sc.eevee.taa_render_samples = op["samples"]
        else:
            # Workbench anti-aliasing is a fixed list of sample counts.
            wanted = op["samples"]
            options = [(int(x), x) for x in _WORKBENCH_AA if x.isdigit()]
            sc.display.render_aa = min(options, key=lambda t: abs(t[0] - wanted))[1]
    if "denoise" in op:
        if C.logical_engine(sc.render.engine) == "cycles":
            sc.cycles.use_denoising = op["denoise"]
        else:
            State.warnings.append("set_render.denoise only applies to the Cycles engine; ignored")
    if "device" in op:
        if C.logical_engine(sc.render.engine) != "cycles":
            State.warnings.append("set_render.device only applies to the Cycles engine; ignored")
        elif op["device"] == "gpu":
            kind = _enable_gpu()
            if kind:
                sc.cycles.device = "GPU"
                info["gpu"] = kind
            else:
                sc.cycles.device = "CPU"
                State.warnings.append("set_render.device gpu: no usable GPU found, rendering on the CPU")
        else:
            sc.cycles.device = "CPU"
    if "transparent" in op:
        sc.render.film_transparent = op["transparent"]
    if "view_transform" in op:
        # The list of transforms comes from the colour configuration, so the
        # only reliable test is to try each candidate in turn.
        chosen = None
        for candidate in _VIEW_TRANSFORMS[op["view_transform"]]:
            try:
                sc.view_settings.view_transform = candidate
            except (TypeError, ValueError):
                continue
            chosen = candidate
            break
        if chosen is None:
            State.warnings.append(f"view_transform '{op['view_transform']}' is not available in Blender "
                                  f"{'.'.join(map(str, VERSION))}; left unchanged")
        else:
            sc.view_settings.view_transform = chosen
            if chosen != _VIEW_TRANSFORMS[op["view_transform"]][0]:
                State.warnings.append(f"view_transform '{op['view_transform']}' replaced by '{chosen}' on this version")
            info["view_transform"] = chosen
    return info


def op_render(op):
    sc = scene()
    if sc.camera is None:
        raise OpError("no active camera: add one with add_camera (make_active) before render")
    path = path_of(op["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    jpeg = path.lower().endswith((".jpg", ".jpeg"))
    img = sc.render.image_settings
    img.file_format = "JPEG" if jpeg else "PNG"
    if jpeg:
        img.quality = op.get("quality", 90)
        img.color_mode = "RGB"
    else:
        img.color_mode = "RGBA" if sc.render.film_transparent else "RGB"
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    if not os.path.isfile(path):
        raise OpError("the renderer finished but no image was written")
    size = record_output(path, "render")
    if size <= 0:
        raise OpError("the rendered image is empty")
    return {"bytes": size, "resolution": [sc.render.resolution_x, sc.render.resolution_y],
            "engine": sc.render.engine}


_GLARE = {"bloom": "BLOOM", "fog_glow": "FOG_GLOW", "streaks": "STREAKS", "ghosts": "GHOSTS",
          "simple_star": "SIMPLE_STAR"}


def _compositor_tree(sc):
    """The scene's compositor node tree on any supported version."""
    if hasattr(sc, "compositing_node_group"):             # 5.0+
        group = sc.compositing_node_group
        if group is None:
            group = bpy.data.node_groups.new("Compositing", "CompositorNodeTree")
            sc.compositing_node_group = group
        return group
    sc.use_nodes = True
    return sc.node_tree


def op_compositor_glare(op):
    sc = scene()
    tree = _compositor_tree(sc)
    for node in list(tree.nodes):
        tree.nodes.remove(node)
    layers = tree.nodes.new("CompositorNodeRLayers")
    glare = tree.nodes.new("CompositorNodeGlare")
    wanted = _GLARE[op.get("glare_type", "fog_glow")]
    kinds = {i.identifier for i in glare.bl_rna.properties["glare_type"].enum_items} \
        if "glare_type" in glare.bl_rna.properties else set()
    if kinds and wanted not in kinds:
        State.warnings.append(f"glare type {wanted} is not available in Blender "
                              f"{'.'.join(map(str, VERSION))}; using FOG_GLOW")
        wanted = "FOG_GLOW"
    if "glare_type" in glare.bl_rna.properties:
        glare.glare_type = wanted
    if "quality" in glare.bl_rna.properties:
        glare.quality = op.get("quality", "high").upper()
    for prop, key, default in (("threshold", "threshold", 1.0), ("mix", "mix", 0.0), ("size", "size", 8)):
        value = op.get(key, default)
        if prop in glare.bl_rna.properties:
            setattr(glare, prop, value)
        else:  # 5.0 moved node settings to sockets
            pick_input(glare, (prop.capitalize(),), value)
    if hasattr(sc, "compositing_node_group") and getattr(tree, "interface", None) is not None:
        tree.interface.new_socket("Image", in_out="OUTPUT", socket_type="NodeSocketColor")
        out = tree.nodes.new("NodeGroupOutput")
    else:
        out = tree.nodes.new("CompositorNodeComposite")
    src = layers.outputs["Image"]
    dst_in = glare.inputs["Image"]
    tree.links.new(src, dst_in)
    tree.links.new(glare.outputs["Image"], out.inputs[0])
    return {"glare": wanted}


def _new_roots(before):
    return [o for o in bpy.data.objects if o.name not in before and o.parent is None]


def op_import_model(op):
    path = path_of(op["path"])
    if not os.path.isfile(path):
        raise OpError(f"file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    fmt = {".stl": "stl", ".obj": "obj", ".fbx": "fbx", ".gltf": "gltf", ".glb": "gltf", ".ply": "ply"}[ext]
    before = {o.name for o in bpy.data.objects}
    if fmt == "stl":
        if has_op("wm.stl_import"):
            bpy.ops.wm.stl_import(filepath=path)
        else:
            bpy.ops.import_mesh.stl(filepath=path)
    elif fmt == "obj":
        if has_op("wm.obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif fmt == "ply":
        if has_op("wm.ply_import"):
            bpy.ops.wm.ply_import(filepath=path)
        else:
            bpy.ops.import_mesh.ply(filepath=path)
    elif fmt == "fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    else:
        bpy.ops.import_scene.gltf(filepath=path)
    roots = _new_roots(before)
    new_total = len([o for o in bpy.data.objects if o.name not in before])
    if new_total == 0:
        raise OpError("the importer added no objects")
    for ob in roots:
        apply_transform(ob, op)
    return {"imported": new_total, "roots": [o.name for o in roots][:10]}


def op_export_model(op):
    path = path_of(op["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    pattern = op.get("target")
    chosen = [o for o in scene().objects
              if (fnmatch.fnmatchcase(o.name, pattern) if pattern else o.type == "MESH")]
    if not chosen:
        raise OpError(f"nothing to export: no object matches {pattern!r}" if pattern else
                      "nothing to export: the scene has no mesh objects")
    selected_only(chosen)
    mods = bool(op.get("apply_modifiers", True))
    if ext == ".stl":
        if has_op("wm.stl_export"):
            bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True, apply_modifiers=mods)
        else:
            bpy.ops.export_mesh.stl(filepath=path, use_selection=True, use_mesh_modifiers=mods)
    elif ext == ".obj":
        if has_op("wm.obj_export"):
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True, apply_modifiers=mods)
        else:
            bpy.ops.export_scene.obj(filepath=path, use_selection=True, use_mesh_modifiers=mods)
    elif ext == ".fbx":
        bpy.ops.export_scene.fbx(filepath=path, use_selection=True)
    elif ext in (".gltf", ".glb"):
        bpy.ops.export_scene.gltf(filepath=path, use_selection=True, export_apply=mods,
                                  export_format="GLB" if ext == ".glb" else "GLTF_EMBEDDED")
    else:
        raise OpError(f"unsupported export extension {ext}")
    if not os.path.isfile(path):
        raise OpError("the exporter finished but no file was written")
    return {"bytes": record_output(path, "model"), "objects": len(chosen)}


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------

def _world_bbox(ob):
    from mathutils import Vector
    bpy.context.view_layer.update()
    try:
        ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
        corners = [ob.matrix_world @ Vector(c) for c in ev.bound_box]
    except Exception:
        corners = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    return ([min(c[i] for c in corners) for i in range(3)],
            [max(c[i] for c in corners) for i in range(3)])


def _fmt(vec):
    return [round(float(v), 5) for v in vec]


def _compare(cmp, observed, expected):
    return {"eq": observed == expected, "ge": observed >= expected, "le": observed <= expected}[cmp]


def _assert(op):
    """Return ``(passed, observed, expected, message)``."""
    sc = scene()
    kind = op["kind"]
    if kind == "object_count":
        want = OBJECT_TYPES.get(op.get("object_type", ""), None)
        count = len([o for o in sc.objects if want is None or o.type == want])
        cmp = op.get("cmp", "eq")
        label = f" of type {op['object_type']}" if want else ""
        return (_compare(cmp, count, op["count"]), count, f"{cmp} {op['count']}",
                f"{count} object(s){label}, expected {cmp} {op['count']}")
    if kind == "object_exists":
        names = [o.name for o in sc.objects if fnmatch.fnmatchcase(o.name, op["name"])]
        return bool(names), names[:10], f"an object matching {op['name']!r}", \
            (f"found {names[:5]}" if names else f"no object matches {op['name']!r}")
    if kind == "material_assigned":
        ob = bpy.data.objects.get(op["object"])
        if ob is None:
            return False, None, op["material"], f"object '{op['object']}' does not exist"
        mats = [s.material.name for s in ob.material_slots if s.material]
        return op["material"] in mats, mats, op["material"], f"'{op['object']}' has materials {mats}"
    if kind == "resolution":
        got = [sc.render.resolution_x, sc.render.resolution_y]
        want = [op["width"], op["height"]]
        return got == want, got, want, f"resolution is {got[0]}x{got[1]}, expected {want[0]}x{want[1]}"
    if kind == "engine":
        got = C.logical_engine(sc.render.engine)
        return got == op["engine"], got, op["engine"], \
            f"engine is {sc.render.engine} ({got}), expected {op['engine']}"
    if kind == "camera_active":
        cam = sc.camera
        if cam is None:
            return False, None, op.get("name", "any camera"), "the scene has no active camera"
        if "name" in op:
            ok = fnmatch.fnmatchcase(cam.name, op["name"])
            return ok, cam.name, op["name"], f"active camera is '{cam.name}', expected {op['name']!r}"
        return True, cam.name, "an active camera", f"active camera is '{cam.name}'"
    if kind == "bbox_within":
        ob = bpy.data.objects.get(op["object"])
        if ob is None:
            return False, None, None, f"object '{op['object']}' does not exist"
        lo, hi = _world_bbox(ob)
        tol = op.get("tolerance", 1e-4)
        ok = True
        problems = []
        if "min" in op:
            for i, axis in enumerate("xyz"):
                if lo[i] < op["min"][i] - tol:
                    ok = False
                    problems.append(f"min {axis} {lo[i]:.4f} < {op['min'][i]}")
        if "max" in op:
            for i, axis in enumerate("xyz"):
                if hi[i] > op["max"][i] + tol:
                    ok = False
                    problems.append(f"max {axis} {hi[i]:.4f} > {op['max'][i]}")
        return ok, {"min": _fmt(lo), "max": _fmt(hi)}, {"min": op.get("min"), "max": op.get("max")}, \
            ("; ".join(problems) if problems else "bounding box is inside the allowed box")
    if kind == "file_exists":
        path = path_of(op["path"])
        size = os.path.getsize(path) if os.path.isfile(path) else None
        need = op.get("min_bytes", 1)
        ok = size is not None and size >= need
        return ok, size, f">= {need} bytes", (f"{path} is {size} bytes" if size is not None else f"{path} does not exist")
    raise OpError(f"unknown assertion kind '{kind}'")


def op_assert(op):
    passed, observed, expected, message = _assert(op)
    State.assertions.append({
        "index": State.current_index, "kind": op["kind"], "label": op.get("label") or op["kind"],
        "passed": bool(passed), "observed": observed, "expected": expected, "message": message})
    if not passed:
        raise OpError(f"assertion failed ({op['kind']}): {message}")
    return {"observed": observed}


HANDLERS = {
    "new_scene": op_new_scene, "load_blend": op_load_blend, "save": op_save, "delete": op_delete,
    "set_world": op_set_world, "add_mesh": op_add_mesh, "set_transform": op_set_transform,
    "add_empty": op_add_empty, "add_material": op_add_material, "assign_material": op_assign_material,
    "add_modifier": op_add_modifier, "add_light": op_add_light, "add_camera": op_add_camera,
    "track_to": op_track_to, "add_text": op_add_text, "set_render": op_set_render, "render": op_render,
    "compositor_glare": op_compositor_glare, "import_model": op_import_model,
    "export_model": op_export_model, "assert": op_assert,
}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def _jsonable(value):
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def emit(report):
    sys.stdout.write("\n" + C.REPORT_PREFIX + json.dumps(report, default=str, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    started = time.time()
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    report = {"schema": 1, "blender": {"version": ".".join(map(str, VERSION))},
              "ok": False, "errors": 0, "ops": [], "assertions": [], "outputs": [], "warnings": []}
    try:
        if len(argv) < 2:
            raise OpError("usage: blender_runner.py -- <scene.json> <root>")
        State.root = os.path.abspath(argv[1])
        with open(argv[0], "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        ops = doc.get("ops") or []
        meta = doc.get("meta") or {}
        report["name"] = meta.get("name")
        stop = bool(meta.get("stop_on_error"))
        reset_empty()
        stopped = False
        for index, op in enumerate(ops):
            name = op.get("op", "?")
            entry = {"index": index, "op": name}
            if stopped:
                entry.update(status="skipped", message="skipped after an earlier error (stop_on_error)", duration_s=0.0)
                report["ops"].append(entry)
                continue
            State.current_index = index
            t0 = time.time()
            try:
                handler = HANDLERS.get(name)
                if handler is None:
                    raise OpError(f"unknown op '{name}'")
                info = handler(op)
                entry["status"] = "ok"
                if info:
                    entry["info"] = _jsonable(info)
            except OpError as exc:
                entry.update(status="error", message=str(exc))
            except Exception as exc:  # noqa: BLE001 - one bad op must not end the run
                tb = traceback.extract_tb(exc.__traceback__)
                where = f" [{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}]" if tb else ""
                entry.update(status="error", message=f"{type(exc).__name__}: {exc}{where}")
            entry["duration_s"] = round(time.time() - t0, 3)
            if entry["status"] == "error":
                report["errors"] += 1
                if stop:
                    stopped = True
            report["ops"].append(entry)
    except Exception as exc:  # noqa: BLE001
        report["errors"] += 1
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-2000:]
    report["assertions"] = State.assertions
    report["outputs"] = State.outputs
    report["warnings"] = State.warnings
    report["duration_s"] = round(time.time() - started, 3)
    report["ok"] = report["errors"] == 0
    emit(report)
    sys.stdout.flush()
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
