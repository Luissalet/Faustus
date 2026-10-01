"""The scene format as data: every op, every field, its type and its range.

One table drives three things so they cannot drift apart: the validator
(`validate.py`), the JSON Schema served to the model (`scene_schema()`) and the
per-op documentation. There is no free-form code op: an op is a name plus typed
parameters, and anything not listed here is rejected before Blender starts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

MAX_OPS = 400
MAX_NAME_LEN = 63          # Blender's ID name limit
MAX_PATH_LEN = 1024
MAX_TEXT_LEN = 2000
MAX_PIXELS = 50_000_000    # width * height

LOC_LIMIT = 100_000.0
ROT_LIMIT = 36_000.0       # degrees
SCALE_LIMIT = 10_000.0

OBJECT_TYPES = {
    "mesh": "MESH", "light": "LIGHT", "camera": "CAMERA", "empty": "EMPTY",
    "text": "FONT", "curve": "CURVE", "armature": "ARMATURE", "surface": "SURFACE",
    "lattice": "LATTICE", "grease_pencil": "GPENCIL",
}
PRIMITIVES = ("cube", "uv_sphere", "ico_sphere", "cylinder", "cone", "plane", "torus", "monkey")
MODIFIERS = ("bevel", "subdivision", "solidify", "array", "mirror")
LIGHT_TYPES = ("point", "sun", "spot", "area")
ENGINES = ("eevee", "cycles", "workbench")
VIEW_TRANSFORMS = ("standard", "filmic", "agx", "raw", "neutral")
GLARE_TYPES = ("bloom", "fog_glow", "streaks", "ghosts", "simple_star")
IMPORT_FORMATS = ("stl", "obj", "fbx", "gltf", "ply")
EXPORT_FORMATS = ("stl", "obj", "fbx", "gltf")
#: extension -> logical format
MODEL_EXTENSIONS = {
    ".stl": "stl", ".obj": "obj", ".fbx": "fbx", ".gltf": "gltf", ".glb": "gltf", ".ply": "ply",
}
ASSERT_KINDS = ("object_count", "object_exists", "material_assigned", "resolution",
                "engine", "camera_active", "bbox_within", "file_exists")


@dataclass(frozen=True)
class Field:
    kind: str                       # str name pattern num int bool vec3 vec2i color path enum axes
    doc: str = ""
    required: bool = False
    min: Optional[float] = None
    max: Optional[float] = None
    enum: Tuple[str, ...] = ()
    max_len: int = MAX_NAME_LEN
    default: Any = None             # documentation only; the runner applies defaults
    exts: Tuple[str, ...] = ()      # for path fields
    when: Tuple[str, ...] = ()      # discriminator values this field applies to (empty = all)


@dataclass(frozen=True)
class OpSpec:
    name: str
    doc: str
    fields: Dict[str, Field] = field(default_factory=dict)
    discriminator: str = ""         # field whose value gates Field.when
    at_least_one_of: Tuple[str, ...] = ()
    check: Optional[Callable[[dict, Callable[[str, str], None], dict], None]] = None


# --------------------------------------------------------------------------
# Field shorthands
# --------------------------------------------------------------------------

def _name(doc, required=False, **kw): return Field("name", doc, required, **kw)
def _num(doc, lo, hi, default=None, **kw): return Field("num", doc, min=lo, max=hi, default=default, **kw)
def _int(doc, lo, hi, default=None, **kw): return Field("int", doc, min=lo, max=hi, default=default, **kw)
def _bool(doc, default=None, **kw): return Field("bool", doc, default=default, **kw)
def _enum(doc, values, required=False, default=None, **kw): return Field("enum", doc, required, enum=tuple(values), default=default, **kw)
def _vec3(doc, lo, hi, default=None, **kw): return Field("vec3", doc, min=lo, max=hi, default=default, **kw)
def _color(doc, default=None, **kw): return Field("color", doc, default=default, **kw)
def _path(doc, exts=(), required=False, **kw): return Field("path", doc, required, exts=tuple(exts), max_len=MAX_PATH_LEN, **kw)

_LOCATION = _vec3("World position in metres [x, y, z].", -LOC_LIMIT, LOC_LIMIT, [0, 0, 0])
_ROTATION = _vec3("Euler rotation in degrees [x, y, z].", -ROT_LIMIT, ROT_LIMIT, [0, 0, 0])
_SCALE = _vec3("Scale [x, y, z].", -SCALE_LIMIT, SCALE_LIMIT, [1, 1, 1])
_NOTE = Field("str", "Free comment for the author; ignored.", max_len=MAX_TEXT_LEN)

# --------------------------------------------------------------------------
# Cross-field checks
# --------------------------------------------------------------------------

_MESH_PARAMS = {
    "size": ("cube", "plane", "monkey"),
    "radius": ("uv_sphere", "ico_sphere", "cylinder", "cone"),
    "radius2": ("cone",),
    "depth": ("cylinder", "cone"),
    "segments": ("uv_sphere", "cylinder", "cone"),
    "rings": ("uv_sphere",),
    "subdivisions": ("ico_sphere",),
    "major_radius": ("torus",),
    "minor_radius": ("torus",),
    "major_segments": ("torus",),
    "minor_segments": ("torus",),
}

_MODIFIER_PARAMS = {
    "width": ("bevel",), "bevel_segments": ("bevel",), "angle_limit": ("bevel",),
    "levels": ("subdivision",), "render_levels": ("subdivision",),
    "thickness": ("solidify",), "offset": ("solidify",),
    "count": ("array",), "relative_offset": ("array",), "constant_offset": ("array",),
    "axes": ("mirror",), "clip": ("mirror",),
}

#: assert kind -> (required fields, optional fields)
_ASSERT_FIELDS = {
    "object_count": (("count",), ("object_type", "cmp")),
    "object_exists": (("name",), ()),
    "material_assigned": (("object", "material"), ()),
    "resolution": (("width", "height"), ()),
    "engine": (("engine",), ()),
    "camera_active": ((), ("name",)),
    "bbox_within": (("object",), ("min", "max", "tolerance")),
    "file_exists": (("path",), ("min_bytes",)),
}
_ASSERT_ALL = {f for req, opt in _ASSERT_FIELDS.values() for f in req + opt}


def _check_mesh(op: dict, err, raw: dict) -> None:
    prim = op.get("primitive")
    for fname, allowed in _MESH_PARAMS.items():
        if fname in raw and prim is not None and prim not in allowed:
            err(fname, f"does not apply to primitive '{prim}' (applies to: {', '.join(allowed)})")


def _check_modifier(op: dict, err, raw: dict) -> None:
    kind = op.get("type")
    for fname, allowed in _MODIFIER_PARAMS.items():
        if fname in raw and kind is not None and kind not in allowed:
            err(fname, f"does not apply to modifier '{kind}' (applies to: {', '.join(allowed)})")


def _check_light(op: dict, err, raw: dict) -> None:
    kind = op.get("type")
    for fname in ("spot_angle", "spot_blend"):
        if fname in raw and kind is not None and kind != "spot":
            err(fname, f"only applies to type 'spot', not '{kind}'")


def _check_camera(op: dict, err, raw: dict) -> None:
    if "dof_focus_object" in raw and "dof_focus_distance" in raw:
        err("dof_focus_distance", "give either dof_focus_object or dof_focus_distance, not both")
    if "ortho_scale" in raw and op.get("projection") == "perspective":
        err("ortho_scale", "only applies to projection 'orthographic'")


def _check_render_settings(op: dict, err, raw: dict) -> None:
    res = op.get("resolution")
    if isinstance(res, list) and len(res) == 2 and res[0] * res[1] > MAX_PIXELS:
        err("resolution", f"{res[0]}x{res[1]} is more than {MAX_PIXELS} pixels")


def _model_format(path: str, declared: Optional[str], formats: Tuple[str, ...], err) -> None:
    import posixpath
    ext = posixpath.splitext(str(path).replace("\\", "/"))[1].lower()
    fmt = MODEL_EXTENSIONS.get(ext)
    if fmt is None or fmt not in formats:
        err("path", f"unsupported model extension '{ext}' (expected one of: "
                    + ", ".join(e for e, f in MODEL_EXTENSIONS.items() if f in formats) + ")")
    elif declared and declared != fmt:
        err("format", f"'{declared}' does not match the file extension '{ext}' ({fmt})")


def _check_import(op: dict, err, raw: dict) -> None:
    if "path" in op:
        _model_format(op["path"], op.get("format"), IMPORT_FORMATS, err)


def _check_export(op: dict, err, raw: dict) -> None:
    if "path" in op:
        _model_format(op["path"], op.get("format"), EXPORT_FORMATS, err)


def _check_assert(op: dict, err, raw: dict) -> None:
    kind = op.get("kind")
    if kind not in _ASSERT_FIELDS:
        return
    required, optional = _ASSERT_FIELDS[kind]
    for fname in required:
        if fname not in raw:
            err(fname, f"is required for kind '{kind}'")
    allowed = set(required) | set(optional)
    for fname in _ASSERT_ALL - allowed:
        if fname in raw:
            err(fname, f"does not apply to kind '{kind}' (allowed: {', '.join(sorted(allowed)) or 'none'})")
    if kind == "bbox_within" and "min" not in raw and "max" not in raw:
        err("min", "give min and/or max for kind 'bbox_within'")


# --------------------------------------------------------------------------
# The ops
# --------------------------------------------------------------------------

def _build() -> Dict[str, OpSpec]:
    ops: List[OpSpec] = []

    def add(name, doc, fields=None, **kw):
        merged = dict(fields or {})
        merged["note"] = _NOTE
        ops.append(OpSpec(name, doc, merged, **kw))

    add("new_scene", "Clear everything and start from an empty scene. The run already starts empty; "
        "use this to start over in the middle of a scene.")
    add("load_blend", "Open an existing .blend file inside the project folder, replacing the current scene.",
        {"path": _path("The .blend file to open.", (".blend",), required=True)})
    add("save", "Save the current scene as a .blend file inside the project folder.",
        {"path": _path("Destination .blend file.", (".blend",), required=True),
         "compress": _bool("Compress the file.", False)})
    add("delete", "Delete objects whose name matches a glob pattern (case-sensitive).",
        {"pattern": Field("pattern", "fnmatch pattern such as 'Cube*' or '*'.", True, max_len=100),
         "object_type": _enum("Only delete objects of this type.", OBJECT_TYPES),
         "must_match": _bool("Fail when nothing matched.", False)})
    add("set_world", "Set the world background: a flat colour and strength, or an HDRI image.",
        {"color": _color("Background colour: [r, g, b(, a)] linear 0-1 or '#rrggbb' (sRGB).", [0.05, 0.05, 0.05, 1]),
         "strength": _num("Background strength.", 0, 1000, 1.0),
         "hdri": _path("Environment image (.hdr, .exr, .png, .jpg).", (".hdr", ".exr", ".png", ".jpg", ".jpeg")),
         "hdri_rotation": _num("Rotation of the HDRI around Z in degrees.", -ROT_LIMIT, ROT_LIMIT, 0)})
    add("add_mesh", "Add a primitive mesh. Size parameters apply only to the primitives listed per field.",
        {"primitive": _enum("Primitive kind.", PRIMITIVES, required=True),
         "name": _name("Object name; must not already exist."),
         "location": _LOCATION, "rotation": _ROTATION, "scale": _SCALE,
         "size": _num("Edge length (cube, plane) or size (monkey).", 1e-4, 1e5, 2.0),
         "radius": _num("Radius (uv_sphere, ico_sphere, cylinder, cone).", 1e-4, 1e5, 1.0),
         "radius2": _num("Radius of the top of a cone.", 0, 1e5, 0.0),
         "depth": _num("Height (cylinder, cone).", 1e-4, 1e5, 2.0),
         "segments": _int("Segments around the axis (uv_sphere, cylinder, cone).", 3, 256, 32),
         "rings": _int("Rings (uv_sphere).", 2, 256, 16),
         "subdivisions": _int("Subdivisions (ico_sphere).", 1, 7, 2),
         "major_radius": _num("Torus major radius.", 1e-4, 1e5, 1.0),
         "minor_radius": _num("Torus minor radius.", 1e-4, 1e5, 0.25),
         "major_segments": _int("Torus major segments.", 3, 256, 48),
         "minor_segments": _int("Torus minor segments.", 3, 128, 12),
         "smooth": _bool("Smooth shading.", False),
         "material": _name("Name of a material created earlier with add_material.")},
        check=_check_mesh)
    add("set_transform", "Set the location, rotation and/or scale of an existing object.",
        {"target": _name("Object name.", required=True),
         "location": _LOCATION, "rotation": _ROTATION, "scale": _SCALE},
        at_least_one_of=("location", "rotation", "scale"))
    add("add_empty", "Add an empty (a transform-only object, useful as a target or pivot).",
        {"name": _name("Object name; must not already exist."),
         "location": _LOCATION, "rotation": _ROTATION, "scale": _SCALE,
         "display": _enum("Display shape.", ("plain_axes", "arrows", "single_arrow", "circle", "cube", "sphere", "cone"), default="plain_axes"),
         "size": _num("Display size.", 1e-4, 1e5, 1.0)})
    add("add_material", "Create a Principled BSDF material.",
        {"name": _name("Material name; must not already exist.", required=True),
         "base_color": _color("Base colour.", [0.8, 0.8, 0.8, 1]),
         "metallic": _num("Metallic.", 0, 1, 0.0),
         "roughness": _num("Roughness.", 0, 1, 0.5),
         "emission_color": _color("Emission colour."),
         "emission_strength": _num("Emission strength.", 0, 10000, 0.0),
         "alpha": _num("Opacity (below 1 enables blending).", 0, 1, 1.0),
         "transmission": _num("Transmission (glass-like).", 0, 1, 0.0),
         "ior": _num("Index of refraction.", 1.0, 3.0, 1.45)})
    add("assign_material", "Assign an existing material to an object (slot 0 unless a slot is given).",
        {"target": _name("Object name.", required=True),
         "material": _name("Material name.", required=True),
         "slot": _int("Material slot index.", 0, 31, 0)})
    add("add_modifier", "Add a modifier to an object. Parameters apply only to their own modifier type.",
        {"target": _name("Object name.", required=True),
         "type": _enum("Modifier type.", MODIFIERS, required=True),
         "name": _name("Modifier name."),
         "width": _num("Bevel width.", 0, 100, 0.1),
         "bevel_segments": _int("Bevel segments.", 1, 32, 1),
         "angle_limit": _num("Bevel only edges sharper than this angle in degrees.", 0, 180, 30),
         "levels": _int("Viewport subdivision levels.", 0, 6, 1),
         "render_levels": _int("Render subdivision levels.", 0, 6, 2),
         "thickness": _num("Solidify thickness.", -100, 100, 0.01),
         "offset": _num("Solidify offset (-1 inside, 1 outside).", -1, 1, -1),
         "count": _int("Array copies.", 1, 1000, 2),
         "relative_offset": _vec3("Array offset relative to the object size.", -1000, 1000, [1, 0, 0]),
         "constant_offset": _vec3("Array offset in metres.", -LOC_LIMIT, LOC_LIMIT),
         "axes": Field("axes", "Mirror axes, a list of 'x', 'y', 'z'.", default=["x"]),
         "clip": _bool("Mirror clipping.", False)},
        discriminator="type", check=_check_modifier)
    add("add_light", "Add a light. energy is watts for point, spot and area, W/m2 for sun.",
        {"type": _enum("Light type.", LIGHT_TYPES, required=True),
         "name": _name("Object name; must not already exist."),
         "location": _LOCATION, "rotation": _ROTATION,
         "energy": _num("Power; defaults per type (point 1000, sun 3, spot 1000, area 500).", 0, 1e7),
         "color": _color("Light colour.", [1, 1, 1, 1]),
         "size": _num("Point/spot radius or area edge length in metres; sun angular diameter in degrees.", 0, 1000),
         "spot_angle": _num("Spot cone angle in degrees.", 1, 180, 45),
         "spot_blend": _num("Spot edge softness.", 0, 1, 0.15)},
        check=_check_light)
    add("add_camera", "Add a camera and (by default) make it the active one.",
        {"name": _name("Object name; must not already exist."),
         "location": _LOCATION, "rotation": _ROTATION,
         "lens": _num("Focal length in millimetres.", 1, 5000, 50),
         "sensor_width": _num("Sensor width in millimetres.", 1, 100, 36),
         "projection": _enum("Projection.", ("perspective", "orthographic"), default="perspective"),
         "ortho_scale": _num("Orthographic scale.", 1e-3, 1e5, 6.0),
         "dof_focus_object": _name("Object to keep in focus."),
         "dof_focus_distance": _num("Focus distance in metres.", 1e-3, 1e5),
         "f_stop": _num("Aperture f-stop; enables depth of field.", 0.1, 128),
         "make_active": _bool("Make this the scene camera.", True)},
        check=_check_camera)
    add("track_to", "Make an object always point at another one (a Track To constraint).",
        {"object": _name("Object that turns (usually a camera or light).", required=True),
         "target": _name("Object to look at.", required=True)})
    add("add_text", "Add 3D text.",
        {"body": Field("str", "The text.", True, max_len=500),
         "name": _name("Object name; must not already exist."),
         "location": _LOCATION, "rotation": _ROTATION, "scale": _SCALE,
         "size": _num("Letter size in metres.", 1e-3, 1000, 1.0),
         "extrude": _num("Extrusion depth in metres.", 0, 100, 0.0),
         "material": _name("Name of a material created earlier.")})
    add("set_render", "Configure the renderer. The engine name is mapped to the right internal identifier for the Blender version.",
        {"engine": _enum("Render engine.", ENGINES),
         "resolution": Field("vec2i", "[width, height] in pixels.", default=[1920, 1080]),
         "percentage": _int("Resolution percentage.", 1, 1000, 100),
         "samples": _int("Samples per pixel.", 1, 65536),
         "denoise": _bool("Denoise (Cycles)."),
         "device": _enum("Cycles device; GPU falls back to CPU when none is usable.", ("cpu", "gpu"), default="cpu"),
         "transparent": _bool("Transparent film background.", False),
         "view_transform": _enum("Colour view transform (falls back with a warning when the version lacks it).", VIEW_TRANSFORMS)},
        at_least_one_of=("engine", "resolution", "percentage", "samples", "denoise", "device", "transparent", "view_transform"),
        check=_check_render_settings)
    add("render", "Render the still image from the active camera and write it.",
        {"path": _path("Output .png or .jpg file.", (".png", ".jpg", ".jpeg"), required=True),
         "quality": _int("JPEG quality.", 1, 100, 90)})
    add("compositor_glare", "Add a glare / bloom effect through compositor nodes.",
        {"glare_type": _enum("Glare type (bloom needs Blender 4.2+; older versions use fog_glow).", GLARE_TYPES, default="fog_glow"),
         "threshold": _num("Brightness threshold.", 0, 1000, 1.0),
         "mix": _num("Mix between original (-1) and glare (1).", -1, 1, 0.0),
         "size": _int("Glare size.", 1, 9, 8),
         "quality": _enum("Quality.", ("low", "medium", "high"), default="high")})
    add("import_model", "Import a model file from the project folder into the scene.",
        {"path": _path("Model file (.stl, .obj, .fbx, .gltf, .glb, .ply).", tuple(MODEL_EXTENSIONS), required=True),
         "format": _enum("File format; inferred from the extension when omitted.", IMPORT_FORMATS),
         "location": _LOCATION, "rotation": _ROTATION, "scale": _SCALE},
        check=_check_import)
    add("export_model", "Export objects to a model file inside the project folder.",
        {"path": _path("Destination (.stl, .obj, .fbx, .gltf, .glb).", tuple(e for e, f in MODEL_EXTENSIONS.items() if f in EXPORT_FORMATS), required=True),
         "format": _enum("File format; inferred from the extension when omitted.", EXPORT_FORMATS),
         "target": Field("pattern", "Glob of object names to export (default: all meshes).", max_len=100),
         "apply_modifiers": _bool("Apply modifiers in the export.", True)},
        check=_check_export)
    add("assert", "Check the scene and report observed versus expected. A failed assertion counts as an error.",
        {"kind": _enum("What to check.", ASSERT_KINDS, required=True),
         "label": Field("str", "Name shown in the report.", max_len=200),
         "cmp": _enum("Comparison for object_count.", ("eq", "ge", "le"), default="eq"),
         "count": _int("Expected object count.", 0, 1_000_000),
         "object_type": _enum("Count only this object type.", OBJECT_TYPES),
         "name": Field("pattern", "Object name or glob.", max_len=100),
         "object": _name("Object name."),
         "material": _name("Material name."),
         "width": _int("Expected resolution width.", 1, 16384),
         "height": _int("Expected resolution height.", 1, 16384),
         "engine": _enum("Expected engine.", ENGINES),
         "min": _vec3("Lower corner of the allowed world box.", -LOC_LIMIT, LOC_LIMIT),
         "max": _vec3("Upper corner of the allowed world box.", -LOC_LIMIT, LOC_LIMIT),
         "tolerance": _num("Slack on each side of the box.", 0, 1000, 1e-4),
         "path": _path("File to check.", ()),
         "min_bytes": _int("Minimum file size.", 0, 2**40, 1)},
        check=_check_assert)
    return {o.name: o for o in ops}


OPS: Dict[str, OpSpec] = _build()

META_FIELDS: Dict[str, Field] = {
    "name": Field("str", "Scene name, shown in reports.", max_len=200),
    "description": Field("str", "What the scene is for.", max_len=MAX_TEXT_LEN),
    "stop_on_error": Field("bool", "Stop at the first failing op or assertion (default: continue).", default=False),
}


# --------------------------------------------------------------------------
# JSON Schema
# --------------------------------------------------------------------------

def _field_schema(f: Field) -> dict:
    s: Dict[str, Any]
    if f.kind in ("name", "str", "pattern", "path"):
        s = {"type": "string", "minLength": 1, "maxLength": f.max_len}
        if f.kind == "path":
            s["description_path"] = True
    elif f.kind == "num":
        s = {"type": "number"}
    elif f.kind == "int":
        s = {"type": "integer"}
    elif f.kind == "bool":
        s = {"type": "boolean"}
    elif f.kind == "enum":
        s = {"type": "string", "enum": list(f.enum)}
    elif f.kind == "vec3":
        s = {"type": "array", "items": {"type": "number", "minimum": f.min, "maximum": f.max},
             "minItems": 3, "maxItems": 3}
    elif f.kind == "vec2i":
        s = {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 16384},
             "minItems": 2, "maxItems": 2}
    elif f.kind == "color":
        s = {"oneOf": [
            {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1}, "minItems": 3, "maxItems": 4},
            {"type": "string", "pattern": r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"},
        ]}
    elif f.kind == "axes":
        s = {"type": "array", "items": {"type": "string", "enum": ["x", "y", "z"]},
             "minItems": 1, "maxItems": 3, "uniqueItems": True}
    else:  # pragma: no cover - guarded by the table
        s = {}
    if f.kind in ("num", "int"):
        if f.min is not None:
            s["minimum"] = f.min
        if f.max is not None:
            s["maximum"] = f.max
    s.pop("description_path", None)
    desc = f.doc
    if f.kind == "path":
        desc += " Relative to the project folder or starting with ${PROJECT_ROOT}; must stay inside it."
    if f.default is not None:
        s["default"] = f.default
    if desc:
        s["description"] = desc
    return s


def scene_schema() -> dict:
    """JSON Schema (draft 2020-12) of the whole scene document."""
    variants = []
    for spec in OPS.values():
        props: Dict[str, Any] = {"op": {"const": spec.name}}
        for fname, f in spec.fields.items():
            props[fname] = _field_schema(f)
        required = ["op"] + [n for n, f in spec.fields.items() if f.required]
        variants.append({
            "title": spec.name,
            "description": spec.doc,
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        })
    ops_schema = {"type": "array", "maxItems": MAX_OPS, "items": {"oneOf": variants}}
    meta_props = {k: _field_schema(f) for k, f in META_FIELDS.items()}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Typed 3D scene",
        "description": (
            "A scene is {\"meta\": {...}, \"ops\": [...]} (a bare list of ops is also accepted). Ops run in order "
            "inside Blender in background mode. There is no free-form code. Every string may contain "
            "${PROJECT_ROOT}; every file path must resolve inside the project folder."),
        "oneOf": [
            {"type": "object",
             "properties": {"meta": {"type": "object", "properties": meta_props, "additionalProperties": False},
                            "ops": ops_schema},
             "required": ["ops"], "additionalProperties": False},
            ops_schema,
        ],
    }
