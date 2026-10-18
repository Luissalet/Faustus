"""Native Faustus asset workflows, executed by the official Blender Lab MCP add-on.

No socket server or polling loop is duplicated here. Blender Lab owns transport
and main-thread execution; Faustus owns recipes, persistent jobs and review sheets.
"""
bl_info = {"name": "Faustus", "author": "Luissalet", "version": (1, 0, 0),
           "blender": (5, 1, 0), "location": "View3D > Sidebar > Faustus",
           "description": "Edit the open scene with the local Faustus copilot", "category": "3D View"}

import base64
import contextlib
import hashlib
import io
import importlib
import json
import math
import os
import sys
from pathlib import Path
import time
import traceback
import uuid
import webbrowser

import bpy
from mathutils import Vector

_icons = None
# Canonical HoardLink mark, MIT; embedded PNG retains origin metadata.
_MARK_PNG = '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'


def data_directory():
    explicit = os.getenv("FAUSTUS_BLENDER_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(os.getenv("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "Faustus/blender_bridge"


def stamp():
    return time.time()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def selected(names=None):
    if names is not None:
        return [bpy.data.objects[name] for name in names]
    return [obj for obj in bpy.context.view_layer.objects if obj.select_get()]


def scene_info(names=None):
    scene = bpy.context.scene
    objects = selected(names) if names is not None else list(scene.objects)
    return {"file": bpy.data.filepath, "version": bpy.app.version_string,
            "active": getattr(bpy.context.view_layer.objects.active, "name", None),
            "selected": [obj.name for obj in selected()], "mode": getattr(bpy.context, "mode", "OBJECT"),
            "frame": scene.frame_current, "frame_start": scene.frame_start, "frame_end": scene.frame_end,
            "objects": [{"name": obj.name, "type": obj.type, "parent": getattr(obj.parent, "name", None),
                         "location": list(obj.location), "rotation": list(obj.rotation_euler),
                         "scale": list(obj.scale), "dimensions": list(obj.dimensions),
                         "vertices": len(obj.data.vertices) if obj.type == "MESH" else None,
                         "materials": [slot.material.name if slot.material else None for slot in obj.material_slots],
                         "collections": [col.name for col in obj.users_collection],
                         "modifiers": [{"name": modifier.name, "type": modifier.type} for modifier in obj.modifiers],
                         "vertex_groups": [group.name for group in obj.vertex_groups],
                         "bones": [bone.name for bone in obj.data.bones] if obj.type == "ARMATURE" else [],
                         "action": getattr(getattr(obj.animation_data, "action", None), "name", None)} for obj in objects],
            "actions": [{"name": action.name, "frames": list(action.frame_range)} for action in bpy.data.actions]}


def execute_code(code, job_id):
    output, errors = io.StringIO(), io.StringIO()
    scope = {"bpy": bpy, "math": math, "json": json, "result": None,
             "__name__": "__faustus_scene__"}
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            exec(compile(code, "<faustus:" + job_id + ">", "exec"), scope)
    except Exception:
        return {"ok": False, "stdout": output.getvalue(), "stderr": errors.getvalue(),
                "error": traceback.format_exc()}
    return {"ok": True, "stdout": output.getvalue(), "stderr": errors.getvalue(), "value": scope.get("result")}


def output_file(path):
    target = Path(bpy.path.abspath(path)).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def rna_property(prop):
    result = {"name": prop.identifier, "type": prop.type, "description": prop.description,
              "readonly": prop.is_readonly, "array_length": getattr(prop, "array_length", 0)}
    if prop.type == 'ENUM':
        result["items"] = [{"id": item.identifier, "name": item.name, "description": item.description}
                           for item in prop.enum_items]
    for key in ("default", "default_array", "hard_min", "hard_max"):
        try:
            value = getattr(prop, key)
            result[key] = list(value) if key == "default_array" else value
        except (AttributeError, TypeError):
            pass
    return result


def inspect_api(payload):
    identifier = payload.get("identifier", "bpy.types.Scene")
    parts = identifier.split('.')
    if parts[:2] == ['bpy', 'ops']:
        if len(parts) == 3:
            module = getattr(bpy.ops, parts[2])
            return {"version": bpy.app.version_string, "operators": [identifier + '.' + name for name in dir(module) if not name.startswith('_')]}
        operator = getattr(getattr(bpy.ops, parts[2]), parts[3])
        rna = operator.get_rna_type()
        return {"version": bpy.app.version_string, "identifier": identifier, "available_in_context": operator.poll(),
                "description": rna.description, "properties": [rna_property(prop) for prop in rna.properties if prop.identifier != 'rna_type']}
    if parts[:2] == ['bpy', 'types'] and len(parts) == 3:
        rna = getattr(bpy.types, parts[2]).bl_rna
        return {"version": bpy.app.version_string, "identifier": identifier, "description": rna.description,
                "properties": [rna_property(prop) for prop in rna.properties if prop.identifier != 'rna_type'],
                "functions": [{"name": fn.identifier, "description": fn.description,
                               "parameters": [rna_property(prop) for prop in fn.parameters]} for fn in rna.functions]}
    raise ValueError("Use bpy.types.TypeName, bpy.ops.category or bpy.ops.category.operator")


def node_tree(payload):
    owner = payload.get("owner", "group")
    name = payload.get("name")
    if owner == "group":
        return bpy.data.node_groups[name]
    if owner in ("material", "world", "light"):
        data = {"material": bpy.data.materials, "world": bpy.data.worlds, "light": bpy.data.lights}[owner][name]
        return data.node_tree
    if owner == "compositor":
        scene = bpy.data.scenes[name] if name else bpy.context.scene
        return scene.compositing_node_group
    if owner == "modifier":
        return bpy.data.objects[name].modifiers[payload["modifier"]].node_group
    raise ValueError("Node owner: group, material, world, light, compositor or modifier")


def socket_value(socket):
    result = {"id": socket.identifier, "name": socket.name, "type": socket.bl_idname,
              "linked": socket.is_linked, "enabled": socket.enabled}
    if hasattr(socket, "default_value"):
        value = socket.default_value
        if isinstance(value, (str, bool, float, int)):
            result["value"] = value
        elif isinstance(value, bpy.types.ID):
            result["value"] = {"name": value.name, "type": value.id_type}
        elif value is not None:
            try:
                result["value"] = list(value)
            except TypeError:
                result["value"] = str(value)
    return result


def find_socket(sockets, key):
    if isinstance(key, int):
        return sockets[key]
    return next((socket for socket in sockets if socket.identifier == key), None) or sockets[key]


def edit_nodes(payload):
    tree = node_tree(payload)
    if tree is None:
        raise ValueError("The owner has no node tree")
    applied = []
    for index, operation in enumerate(payload.get("ops", [])):
        try:
            kind = operation["op"]
            if kind == "add":
                node = tree.nodes.new(operation["type"])
                node.name = operation["name"]
            elif kind == "remove":
                tree.nodes.remove(tree.nodes[operation["name"]])
            elif kind == "set":
                node = tree.nodes[operation["name"]]
                for key, value in operation.get("properties", {}).items():
                    setattr(node, key, value)
                for key, value in operation.get("inputs", {}).items():
                    find_socket(node.inputs, key).default_value = value
            elif kind in ("link", "unlink"):
                source = find_socket(tree.nodes[operation["from_node"]].outputs, operation["from_socket"])
                target = find_socket(tree.nodes[operation["to_node"]].inputs, operation["to_socket"])
                if kind == "link":
                    tree.links.new(source, target)
                else:
                    for link in list(tree.links):
                        if link.from_socket == source and link.to_socket == target:
                            tree.links.remove(link)
            else:
                raise ValueError("Node op: add, remove, set, link or unlink")
            applied.append({"index": index, "op": kind})
        except Exception as exc:
            return {"ok": False, "applied": applied, "failed_index": index, "error": str(exc), "tree": tree.name}
    return {"ok": True, "tree": tree.name, "type": tree.bl_idname, "applied": applied,
            "nodes": [{"name": node.name, "type": node.bl_idname, "label": node.label,
                       "location": list(node.location), "parent": getattr(node.parent, 'name', None),
                       "inputs": [socket_value(socket) for socket in node.inputs],
                       "outputs": [socket_value(socket) for socket in node.outputs]} for node in tree.nodes],
            "links": [{"from_node": link.from_node.name, "from_socket": link.from_socket.identifier,
                       "to_node": link.to_node.name, "to_socket": link.to_socket.identifier} for link in tree.links],
            "interface": [{"name": item.name, "type": item.item_type, "identifier": getattr(item, 'identifier', None)} for item in tree.interface.items_tree]}


def inspect_animation(payload):
    obj = bpy.data.objects[payload["object"]]
    data = obj.animation_data
    clips = []
    for action in bpy.data.actions:
        layers = []
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    layers.append({"layer": layer.name, "slot": bag.slot_handle,
                        "curves": [{"path": curve.data_path, "index": curve.array_index,
                                    "keys": [{"co": list(point.co), "interpolation": point.interpolation} for point in curve.keyframe_points]}
                                   for curve in bag.fcurves]})
        clips.append({"name": action.name, "frames": list(action.frame_range),
                      "slots": [{"id": slot.identifier, "type": slot.target_id_type} for slot in action.slots], "layers": layers})
    return {"object": obj.name, "active": getattr(getattr(data, 'action', None), 'name', None), "actions": clips,
            "nla": [{"name": track.name, "mute": track.mute, "solo": track.is_solo,
                     "strips": [{"name": strip.name, "action": getattr(strip.action, 'name', None),
                                 "start": strip.frame_start, "end": strip.frame_end} for strip in track.strips]}
                    for track in data.nla_tracks] if data else [],
            "drivers": [{"path": curve.data_path, "index": curve.array_index, "expression": curve.driver.expression,
                         "variables": [{"name": var.name, "type": var.type,
                                        "targets": [{"id": getattr(target.id, 'name', None), "path": target.data_path} for target in var.targets]}
                                       for var in curve.driver.variables]} for curve in data.drivers] if data else []}


def inspect_asset(payload):
    """Production observations, without changing geometry, pose or selection."""
    import bmesh
    meshes = []
    for obj in selected(payload.get('objects')):
        if obj.type != 'MESH':
            continue
        mesh = obj.data
        topology = bmesh.new()
        try:
            topology.from_mesh(mesh)
            health = {'boundary_edges':sum(edge.is_boundary for edge in topology.edges),
                      'non_manifold_edges':sum(not edge.is_manifold for edge in topology.edges),
                      'wire_edges':sum(edge.is_wire for edge in topology.edges),
                      'zero_area_faces':sum(face.calc_area() <= 1e-12 for face in topology.faces)}
        finally:
            topology.free()
        rigs = [modifier.object for modifier in obj.modifiers if modifier.type == 'ARMATURE' and modifier.object]
        bones = {bone.name for rig in rigs for bone in rig.data.bones if bone.use_deform}
        deform_groups = {group.index for group in obj.vertex_groups if group.name in bones}
        totals = [sum(group.weight for group in vertex.groups if group.group in deform_groups) for vertex in mesh.vertices]
        uv = []
        for layer in mesh.uv_layers:
            points = [tuple(item.vector) for item in layer.uv]
            areas = []
            for face in mesh.polygons:
                polygon = [points[index] for index in face.loop_indices]
                areas.append(abs(sum(polygon[index][0]*polygon[(index+1)%len(polygon)][1] -
                                     polygon[(index+1)%len(polygon)][0]*polygon[index][1]
                                     for index in range(len(polygon))))*.5)
            uv.append({'name':layer.name, 'loops':len(points), 'zero_area_faces':sum(area <= 1e-12 for area in areas),
                       'outside_unit_square':sum(not (0 <= u <= 1 and 0 <= v <= 1) for u,v in points)})
        keys = mesh.shape_keys
        meshes.append({'name':obj.name, 'vertices':len(mesh.vertices), 'edges':len(mesh.edges), 'faces':len(mesh.polygons),
                       'scale':list(obj.scale), 'world_determinant':obj.matrix_world.determinant(), 'topology':health,
                       'uv':uv, 'materials':[getattr(slot.material,'name',None) for slot in obj.material_slots],
                       'armatures':[rig.name for rig in rigs],
                       'weights':{'unweighted_vertices':sum(total <= 1e-8 for total in totals),
                                  'non_normalized_vertices':sum(abs(total-1) > 1e-4 for total in totals)} if rigs else None,
                       'shape_keys':[{'name':key.name,'value':key.value,'mute':key.mute} for key in keys.key_blocks] if keys else []})
    return {'meshes':meshes, 'units':bpy.context.scene.unit_settings.system,
            'unit_scale':bpy.context.scene.unit_settings.scale_length,
            'notes':['Open surfaces, UV coordinates outside 0–1 and non-unit scale can be intentional.',
                     'Reports base mesh data; does not claim intersection or UV-overlap detection.']}


@contextlib.contextmanager
def export_clips(payload):
    """Bind explicit clips to their owner using temporary NLA tracks.

    Blender's ALL ACTIONS option also exports unrelated compatible rigs. Using
    bound tracks preserves clip ownership and leaves every original action intact.
    """
    restored = []
    try:
        for name, clips in payload.get("actions", {}).items():
            obj = bpy.data.objects[name]
            data = obj.animation_data_create()
            original = (data.action, data.action_slot)
            muted = [(track, track.mute) for track in data.nla_tracks]
            added = []
            restored.append((data, original, muted, added))
            data.action = None
            for track, _ in muted:
                track.mute = True
            for clip in clips:
                action = bpy.data.actions[clip]
                track = data.nla_tracks.new()
                added.append(track)
                track.name = action.name
                strip = track.strips.new(action.name, int(action.frame_range[0]), action)
                slot = next((slot for slot in action.slots if slot.target_id_type == obj.id_type), None)
                if slot:
                    strip.action_slot = slot
        yield
    finally:
        for data, (action, slot), muted, added in reversed(restored):
            for track in added:
                data.nla_tracks.remove(track)
            for track, mute in muted:
                track.mute = mute
            data.action = action
            if action and slot:
                data.action_slot = slot


def export_scene(payload):
    target = output_file(payload["path"])
    kind = payload.get("format", target.suffix.lstrip(".")).lower()
    before = selected()
    active = bpy.context.view_layer.objects.active
    try:
        if "objects" in payload:
            for obj in bpy.context.view_layer.objects:
                obj.select_set(False)
            for obj in selected(payload["objects"]):
                obj.select_set(True)
            bpy.context.view_layer.objects.active = next(iter(selected()), None)
        only = bool(payload.get("selected_only", "objects" in payload))
        with export_clips(payload):
            if kind in ("glb", "gltf"):
                bpy.ops.export_scene.gltf(filepath=str(target), export_format="GLB" if kind == "glb" else "GLTF_SEPARATE",
                                          use_selection=only, export_animations=True, export_animation_mode="NLA_TRACKS")
            elif kind == "fbx":
                bpy.ops.export_scene.fbx(filepath=str(target), use_selection=only, bake_anim=True,
                                        bake_anim_use_all_actions=False, bake_anim_use_nla_strips=True, add_leaf_bones=False)
            elif kind == "stl":
                bpy.ops.wm.stl_export(filepath=str(target), export_selected_objects=only)
            elif kind == "obj":
                bpy.ops.wm.obj_export(filepath=str(target), export_selected_objects=only)
            else:
                raise ValueError("Export format must be glb, gltf, fbx, stl or obj")
    finally:
        for obj in bpy.context.view_layer.objects:
            obj.select_set(obj in before)
        bpy.context.view_layer.objects.active = active
    result = {"path": str(target), "bytes": target.stat().st_size, "sha256": sha(target), "format": kind}
    if kind in ("glb", "gltf"):
        if kind == "glb":
            import struct
            with target.open("rb") as stream:
                assert stream.read(4) == b"glTF"
                stream.seek(12)
                length, chunk = struct.unpack("<II", stream.read(8))
                assert chunk == 0x4E4F534A
                document = json.loads(stream.read(length))
        else:
            document = json.loads(target.read_text(encoding="utf-8"))
        result["validation"] = {"nodes": len(document.get("nodes", [])), "meshes": len(document.get("meshes", [])),
                                "skins": len(document.get("skins", [])), "materials": len(document.get("materials", [])),
                                "animations": [row.get("name", "") for row in document.get("animations", [])]}
    return result


def render_views(payload, job, service):
    scene = bpy.context.scene
    objects = selected(payload.get("objects"))
    if not objects:
        objects = [obj for obj in scene.objects if obj.type in ("MESH", "CURVE", "FONT", "SURFACE", "META")]
    if not objects:
        raise ValueError("The scene has no geometry to review")
    angles = payload.get("angles", [45, 135, 225, 315])
    frames = payload.get("frames", [scene.frame_current])
    width, height = int(payload.get("width", 512)), int(payload.get("height", 512))
    if width < 1 or height < 1 or not angles or not frames:
        raise ValueError("Choose positive dimensions and at least one angle and frame")
    directory = service.directory / "reviews" / job["id"]
    directory.mkdir(parents=True)
    settings = {key: getattr(scene.render, key) for key in ("engine", "resolution_x", "resolution_y", "resolution_percentage", "filepath", "film_transparent")}
    image_format = scene.render.image_settings.file_format
    camera, frame = scene.camera, scene.frame_current
    shading = {key: getattr(scene.display.shading, key) for key in ("light", "color_type", "show_shadows", "show_cavity", "background_type", "background_color")}
    temporary_data = bpy.data.cameras.new("Faustus review")
    temporary = bpy.data.objects.new("Faustus review", temporary_data)
    scene.collection.objects.link(temporary)
    images = []
    try:
        scene.camera = temporary
        scene.render.engine = "BLENDER_WORKBENCH"
        scene.render.resolution_x, scene.render.resolution_y = width, height
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.film_transparent = False
        scene.display.shading.light = "STUDIO"
        scene.display.shading.color_type = "MATERIAL"
        scene.display.shading.show_cavity = True
        scene.display.shading.background_type = "WORLD"
        scene.display.shading.background_color = (0.06, 0.07, 0.08)
        for frame_value in frames:
            scene.frame_set(int(frame_value))
            bpy.context.view_layer.update()
            corners = [obj.matrix_world @ Vector(corner) for obj in objects if hasattr(obj, "bound_box") for corner in obj.bound_box]
            minimum = Vector(tuple(min(v[i] for v in corners) for i in range(3)))
            maximum = Vector(tuple(max(v[i] for v in corners) for i in range(3)))
            center = (minimum + maximum) * .5
            radius = max((maximum - minimum).length, .1)
            for angle in angles:
                service.check_cancel(job["id"])
                radians = math.radians(float(angle))
                temporary.location = center + Vector((math.cos(radians), math.sin(radians), .65)) * radius * 1.8
                temporary.rotation_euler = (center - temporary.location).to_track_quat("-Z", "Y").to_euler()
                temporary_data.lens = 50
                target = directory / (str(len(images)) + ".png")
                scene.render.filepath = str(target)
                bpy.ops.render.render(write_still=True)
                images.append({"path": str(target), "frame": int(frame_value), "angle": float(angle), "sha256": sha(target)})
                service.progress(job["id"], {"images": images, "completed": len(images), "total": len(angles) * len(frames)})
    finally:
        scene.camera = camera
        scene.frame_set(frame)
        for key, value in settings.items():
            setattr(scene.render, key, value)
        scene.render.image_settings.file_format = image_format
        for key, value in shading.items():
            setattr(scene.display.shading, key, value)
        bpy.data.objects.remove(temporary, do_unlink=True)
        bpy.data.cameras.remove(temporary_data)
    # Native contact sheet: one row per requested frame, one column per angle.
    import numpy as np
    pixels = np.zeros((height * len(frames), width * len(angles), 4), dtype=np.float32)
    for index, entry in enumerate(images):
        image = bpy.data.images.load(entry["path"], check_existing=False)
        values = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(values)
        y, x = divmod(index, len(angles))
        y = len(frames) - 1 - y
        pixels[y * height:(y + 1) * height, x * width:(x + 1) * width] = values.reshape((height, width, 4))
        bpy.data.images.remove(image)
    sheet = bpy.data.images.new("Faustus review sheet", width=width * len(angles), height=height * len(frames), alpha=True)
    try:
        sheet.pixels.foreach_set(pixels.ravel())
        sheet.filepath_raw = str(directory / "sheet.png")
        sheet.file_format = "PNG"
        sheet.save()
        return {"images": images, "sheet": {"path": sheet.filepath_raw, "sha256": sha(sheet.filepath_raw)},
                "rows": frames, "columns": angles, "restored_frame": frame}
    finally:
        bpy.data.images.remove(sheet)


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, path):
        self.path = Path(path)
        self.directory = self.path.parent.parent

    def progress(self, job_id, result):
        job = json.loads(self.path.read_text(encoding="utf-8"))
        job.update(result=result, updated_at=stamp())
        write_json(self.path, job)

    def check_cancel(self, job_id):
        if self.path.with_suffix(".cancel").exists():
            raise Cancelled("Cancelada entre operaciones de Blender")

    def recipe(self, args, job):
        name = args.get("name", "")
        if not name or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in name):
            raise ValueError("Recipe name uses letters, digits, underscore and hyphen")
        path = self.directory / "recipes" / (name + ".json")
        if job["action"] == "recipe_save":
            steps = args["steps"]
            if not steps or len({row["name"] for row in steps}) != len(steps):
                raise ValueError("Supply distinct named stages")
            for step in steps:
                compile(step["code"], "<recipe:" + step["name"] + ">", "exec")
            recipe = {"name": name, "steps": steps, "updated_at": stamp()}
            recipe["sha256"] = hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()
            write_json(path, recipe)
            return recipe
        recipe = json.loads(path.read_text(encoding="utf-8"))
        requested = args.get("stages")
        if requested is not None and set(requested) - {row["name"] for row in recipe["steps"]}:
            raise ValueError("A requested stage is absent from this recipe")
        steps = [row for row in recipe["steps"] if requested is None or row["name"] in requested]
        receipt = {"recipe": recipe, "stages": [], "file_before": bpy.data.filepath}
        for step in steps:
            self.check_cancel(job["id"])
            result = execute_code(step["code"], job["id"] + ":" + step["name"])
            receipt["stages"].append({"name": step["name"], "code_sha256": hashlib.sha256(step["code"].encode()).hexdigest(), **result})
            self.progress(job["id"], receipt)
            if not result["ok"]:
                raise RuntimeError(result["error"])
        return receipt

    def perform(self, job):
        args = job["arguments"]
        action = job["action"]
        if action == "scene":
            return scene_info(args.get("objects"))
        if action == "execute":
            result = execute_code(args["code"], job["id"])
            if not result["ok"]:
                self.progress(job["id"], result)
                raise RuntimeError(result["error"])
            return result
        if action in ("api", "nodes", "animation", "asset_check"):
            result = {"api": inspect_api, "nodes": edit_nodes, "animation": inspect_animation, "asset_check": inspect_asset}[action](args)
            if result.get("ok") is False:
                self.progress(job["id"], result)
                raise RuntimeError(result["error"])
            return result
        if action == "views":
            return render_views(args, job, self)
        if action == "save":
            target = output_file(args["path"])
            bpy.ops.wm.save_as_mainfile(filepath=str(target))
            return {"path": str(target), "bytes": target.stat().st_size, "sha256": sha(target)}
        if action == "load":
            bpy.ops.wm.open_mainfile(filepath=str(output_file(args["path"])), load_ui=False)
            return scene_info()
        if action == "export":
            return export_scene(args)
        if action == "recipe_list":
            return {"recipes": [json.loads(path.read_text(encoding="utf-8")) for path in (self.directory / "recipes").glob("*.json")]}
        return self.recipe(args, job)


def run_job(path):
    service = Job(path)
    job = json.loads(service.path.read_text(encoding="utf-8"))
    try:
        service.check_cancel(job["id"])
        job.update(state="running", updated_at=stamp(), blender_version=bpy.app.version_string)
        write_json(service.path, job)
        result = service.perform(job)
        service.check_cancel(job["id"])
        job.update(state="finished", result=result, updated_at=stamp())
    except Exception as exc:
        current = json.loads(service.path.read_text(encoding="utf-8"))
        job.update(result=current.get("result", {}), state="cancelled" if isinstance(exc, Cancelled) else "failed",
                   error=str(exc), updated_at=stamp())
    write_json(service.path, job)
    return job


class FAUSTUS_OT_start(bpy.types.Operator):
    bl_idname = "faustus.start_bridge"
    bl_label = "Conectar escena"
    bl_description = "Iniciar el servidor MCP oficial de Blender"

    def execute(self, context):
        try:
            connect_local()
        except (RuntimeError, OSError, ImportError) as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class FAUSTUS_OT_stop(bpy.types.Operator):
    bl_idname = "faustus.stop_bridge"
    bl_label = "Desconectar escena"

    def execute(self, context):
        bpy.ops.blmcp.server_stop()
        return {'FINISHED'}


class FAUSTUS_OT_chat(bpy.types.Operator):
    bl_idname = "faustus.open_chat"
    bl_label = "Faustus"
    bl_description = "Abrir el copilot de Faustus para trabajar en esta escena"

    def execute(self, context):
        webbrowser.open(os.getenv("FAUSTUS_URL", "http://127.0.0.1:7000") + "/?blender=live")
        return {'FINISHED'}


class FAUSTUS_PT_scene(bpy.types.Panel):
    bl_label = "Faustus"
    bl_idname = "FAUSTUS_PT_scene"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Faustus"

    def draw(self, context):
        layout = self.layout
        layout.operator("faustus.open_chat", text="", icon_value=_icons["faustus"].icon_id)
        module = sys.modules.get('bl_ext.user_default.mcp')
        connected = bool(module and module.mcp_to_blender_server.is_running())
        layout.label(text="Escena conectada" if connected else "Escena desconectada")
        layout.label(text="Blender MCP · " + bpy.app.version_string)
        layout.operator("faustus.stop_bridge" if connected else "faustus.start_bridge")
        layout.label(text=str(len(selected())) + " objetos seleccionados")


CLASSES = (FAUSTUS_OT_start, FAUSTUS_OT_stop, FAUSTUS_OT_chat, FAUSTUS_PT_scene)


def connect_local():
    """Start the official engine on loopback, including Blender's offline mode.

    The official preferences operator requires online_access even for localhost.
    Using its public socket/poll APIs lets the copilot work without granting
    internet access to Blender or changing the user's global online preference.
    """
    module = importlib.import_module('bl_ext.user_default.mcp')
    from_module = module.mcp_to_blender_server
    from_module_port = int(os.getenv('BLENDER_MCP_PORT') or os.getenv('FAUSTUS_BLENDER_PORT') or
                           bpy.context.preferences.addons[module.__name__].preferences.port)
    if not from_module.is_running():
        from_module.start('127.0.0.1', from_module_port)
        execution = importlib.import_module(module.__name__ + '.execute_interactive')
        if not bpy.app.timers.is_registered(execution.run):
            bpy.app.timers.register(execution.run, first_interval=.1, persistent=True)
    write_json(data_directory() / 'connection.json', {'service':'blender-lab-mcp', 'port':from_module_port,
               'pid':os.getpid(), 'version':bpy.app.version_string})


def auto_connect():
    try:
        connect_local()
    except (RuntimeError, OSError, ImportError, KeyError):
        # Preferences may still be loading, or another instance owns this port.
        # The panel remains usable for an explicit start after that is resolved.
        pass
    return None


def register():
    global _icons
    import bpy.utils.previews
    icon = data_directory() / "faustus-mark.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(base64.b64decode(_MARK_PNG))
    _icons = bpy.utils.previews.new()
    _icons.load("faustus", str(icon), "IMAGE")
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    if not bpy.app.background:
        bpy.app.timers.register(auto_connect, first_interval=1, persistent=True)


def unregister():
    global _icons
    if bpy.app.timers.is_registered(auto_connect):
        bpy.app.timers.unregister(auto_connect)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    if _icons:
        bpy.utils.previews.remove(_icons)
        _icons = None


if __name__ == "__main__":
    register()
