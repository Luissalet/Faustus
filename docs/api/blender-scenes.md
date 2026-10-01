# Typed 3D scenes rendered with Blender

A 3D scene is written as typed JSON, checked before anything starts, built in
order by a fixed runner inside `blender --background`, rendered to an image,
optionally saved as a `.blend` or exported as a model, and verified with
declarative assertions. The result is a structured report that a model (or a
person) can read: which op ran, how long it took, what failed and why, what was
observed against what was expected, and which files were written.

There is no op that runs code. The only inputs are the op names and fields
listed below; anything else is rejected before Blender is launched.

Code: `src/blender_scene/` (format, validator, discovery, launcher, runner),
agent tool `blender_scene` (`src/agent_tools/blender_scene_tool.py`), built-in
MCP server `blender_scene` (`mcp_servers/blender_scene_server.py`), REST routes
(`routes/blender_scene_routes.py`).

## The scene document

```json
{
  "meta": {"name": "demo", "description": "A cube and a sphere", "stop_on_error": false},
  "ops": [
    {"op": "new_scene"},
    {"op": "add_material", "name": "Blue", "base_color": "#2a7fff", "roughness": 0.35},
    {"op": "add_mesh", "primitive": "cube", "name": "Box", "location": [0, 0, 1], "material": "Blue"},
    {"op": "add_modifier", "target": "Box", "type": "bevel", "width": 0.08, "bevel_segments": 3},
    {"op": "add_light", "type": "sun", "rotation": [50, 0, 30]},
    {"op": "add_camera", "name": "Cam", "location": [6, -6, 4]},
    {"op": "track_to", "object": "Cam", "target": "Box"},
    {"op": "set_render", "engine": "workbench", "resolution": [320, 240], "samples": 8},
    {"op": "render", "path": "${PROJECT_ROOT}/box.png"},
    {"op": "assert", "kind": "file_exists", "path": "box.png"}
  ]
}
```

A bare list of ops is accepted in place of the object. `meta` takes `name`,
`description` and `stop_on_error`; any other key is an error, at every level.

Conventions:

- **Order matters.** Ops run top to bottom. A material has to exist before a mesh
  uses it; a camera has to exist before `render`. A run starts from an empty
  scene, so `new_scene` is only needed to start over in the middle.
- **Names.** Objects and materials are referred to by name. Giving a name that
  already exists is an error (Blender would silently rename it); leave `name` out
  to take Blender's default.
- **Units.** Locations and sizes are metres, rotations are degrees, `[x, y, z]`.
- **Colours.** `[r, g, b]` or `[r, g, b, a]` with linear values from 0 to 1, or a
  `"#rgb"`, `"#rrggbb"` or `"#rrggbbaa"` string, which is read as sRGB and
  converted to linear.
- **Free comment.** Every op accepts a `note` string that is ignored.
- **Limits.** At most 400 ops per scene; names up to 63 characters (Blender's
  limit); `set_render` resolution up to 16384 on a side and 50 million pixels.
- **Failures do not stop the run** unless `meta.stop_on_error` is true: the failing
  op is recorded with its message and the next one runs. Ops after a stop are
  reported as `skipped`.

`scene_schema()` (`src/blender_scene`) returns the same format as a JSON Schema
(draft 2020-12). The `schema` action of the tool and the `blender_scene_schema`
MCP tool serve it, either as the full schema or as a compact text summary of
about 6 000 characters.

## Validation

`validate_scene(scene, root)` runs before Blender is started and reports every
problem it finds, not only the first. Each problem has the op index, the op
name, the field and a reason that is written to be sent back to the model:

```
ops[2] (add_mesh).radius: out of range: -1.0 is not between 0.0001 and 100000
ops[5] (add_mesg): unknown op 'add_mesg' (did you mean 'add_mesh'?); valid ops: new_scene, ...
ops[7] (render).path: '..' segments are not allowed in paths
ops[9] (add_mesh).location: expected 3 numbers [x, y, z], got 2 values
```

What is rejected: an unknown op, an unknown field (with a suggestion), a value of
the wrong type (a string where a number is expected, a boolean as a number, NaN
or infinity), a value outside its range, an enum value that is not listed, a
missing required field, a field that does not apply to the chosen primitive,
modifier or light type, and every path problem described next.

On success the validator also returns the normalised scene: `${PROJECT_ROOT}`
replaced, enums lower-cased, colours as `[r, g, b, a]`, numbers as numbers and
every path made absolute. The runner receives that form.

## Paths and `${PROJECT_ROOT}`

Faustus chooses a working folder, the project root. `${PROJECT_ROOT}` is replaced
with it in **every** string of the scene. Every field that names a file
(`path`, `hdri`) must then resolve to a place inside that folder. A path is
rejected when:

- it has a `..` segment (even one that would stay inside the folder);
- it is absolute and outside the folder, including a sibling whose name only
  starts the same (`/data/s1evil` for the root `/data/s1`), another drive, or a
  UNC share;
- `${PROJECT_ROOT}` appears anywhere but at the very start, or another
  `${VARIABLE}` is used;
- it is empty, contains a NUL character, names the folder itself, or on Windows
  carries an alternate data stream (`a.png:stream`);
- following the symbolic links that exist, its real location is outside the real
  folder.

Relative paths are relative to the project root. Windows paths follow the
Windows rules (case-insensitive, either slash) and are checked the same way on
any platform. The check runs in the validator and again inside the runner, so a
scene file that bypassed validation still cannot write outside the folder.

## Operations

### `new_scene`

Clear everything and start from an empty scene. The run already starts empty; use this to start over in the middle of a scene.

### `load_blend`

Open an existing .blend file inside the project folder, replacing the current scene.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `path` (required) | path | ends in `.blend` |  | The .blend file to open. |

### `save`

Save the current scene as a .blend file inside the project folder.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `path` (required) | path | ends in `.blend` |  | Destination .blend file. |
| `compress` | boolean |  | `false` | Compress the file. |

### `delete`

Delete objects whose name matches a glob pattern (case-sensitive).

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `pattern` (required) | glob | up to 100 characters |  | fnmatch pattern such as 'Cube*' or '*'. |
| `object_type` | choice | `mesh`, `light`, `camera`, `empty`, `text`, `curve`, `armature`, `surface`, `lattice`, `grease_pencil` |  | Only delete objects of this type. |
| `must_match` | boolean |  | `false` | Fail when nothing matched. |

### `set_world`

Set the world background: a flat colour and strength, or an HDRI image.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `color` | color |  | `[0.05, 0.05, 0.05, 1]` | Background colour: [r, g, b(, a)] linear 0-1 or '#rrggbb' (sRGB). |
| `strength` | number | 0 to 1000 | `1.0` | Background strength. |
| `hdri` | path | ends in `.hdr`, `.exr`, `.png`, `.jpg`, `.jpeg` |  | Environment image (.hdr, .exr, .png, .jpg). |
| `hdri_rotation` | number | -36000 to 36000 | `0` | Rotation of the HDRI around Z in degrees. |

### `add_mesh`

Add a primitive mesh. Size parameters apply only to the primitives listed per field.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `primitive` (required) | choice | `cube`, `uv_sphere`, `ico_sphere`, `cylinder`, `cone`, `plane`, `torus`, `monkey` |  | Primitive kind. |
| `name` | name | up to 63 characters |  | Object name; must not already exist. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `scale` | [x, y, z] | each -10000 to 10000 | `[1, 1, 1]` | Scale [x, y, z]. |
| `size` | number | 0.0001 to 100000 | `2.0` | Edge length (cube, plane) or size (monkey). |
| `radius` | number | 0.0001 to 100000 | `1.0` | Radius (uv_sphere, ico_sphere, cylinder, cone). |
| `radius2` | number | 0 to 100000 | `0.0` | Radius of the top of a cone. |
| `depth` | number | 0.0001 to 100000 | `2.0` | Height (cylinder, cone). |
| `segments` | integer | 3 to 256 | `32` | Segments around the axis (uv_sphere, cylinder, cone). |
| `rings` | integer | 2 to 256 | `16` | Rings (uv_sphere). |
| `subdivisions` | integer | 1 to 7 | `2` | Subdivisions (ico_sphere). |
| `major_radius` | number | 0.0001 to 100000 | `1.0` | Torus major radius. |
| `minor_radius` | number | 0.0001 to 100000 | `0.25` | Torus minor radius. |
| `major_segments` | integer | 3 to 256 | `48` | Torus major segments. |
| `minor_segments` | integer | 3 to 128 | `12` | Torus minor segments. |
| `smooth` | boolean |  | `false` | Smooth shading. |
| `material` | name | up to 63 characters |  | Name of a material created earlier with add_material. |

### `set_transform`

Set the location, rotation and/or scale of an existing object.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `target` (required) | name | up to 63 characters |  | Object name. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `scale` | [x, y, z] | each -10000 to 10000 | `[1, 1, 1]` | Scale [x, y, z]. |

At least one of `location`, `rotation`, `scale` is required.

### `add_empty`

Add an empty (a transform-only object, useful as a target or pivot).

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `name` | name | up to 63 characters |  | Object name; must not already exist. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `scale` | [x, y, z] | each -10000 to 10000 | `[1, 1, 1]` | Scale [x, y, z]. |
| `display` | choice | `plain_axes`, `arrows`, `single_arrow`, `circle`, `cube`, `sphere`, `cone` | `plain_axes` | Display shape. |
| `size` | number | 0.0001 to 100000 | `1.0` | Display size. |

### `add_material`

Create a Principled BSDF material.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `name` (required) | name | up to 63 characters |  | Material name; must not already exist. |
| `base_color` | color |  | `[0.8, 0.8, 0.8, 1]` | Base colour. |
| `metallic` | number | 0 to 1 | `0.0` | Metallic. |
| `roughness` | number | 0 to 1 | `0.5` | Roughness. |
| `emission_color` | color |  |  | Emission colour. |
| `emission_strength` | number | 0 to 10000 | `0.0` | Emission strength. |
| `alpha` | number | 0 to 1 | `1.0` | Opacity (below 1 enables blending). |
| `transmission` | number | 0 to 1 | `0.0` | Transmission (glass-like). |
| `ior` | number | 1 to 3 | `1.45` | Index of refraction. |

### `assign_material`

Assign an existing material to an object (slot 0 unless a slot is given).

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `target` (required) | name | up to 63 characters |  | Object name. |
| `material` (required) | name | up to 63 characters |  | Material name. |
| `slot` | integer | 0 to 31 | `0` | Material slot index. |

### `add_modifier`

Add a modifier to an object. Parameters apply only to their own modifier type.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `target` (required) | name | up to 63 characters |  | Object name. |
| `type` (required) | choice | `bevel`, `subdivision`, `solidify`, `array`, `mirror` |  | Modifier type. |
| `name` | name | up to 63 characters |  | Modifier name. |
| `width` | number | 0 to 100 | `0.1` | Bevel width. |
| `bevel_segments` | integer | 1 to 32 | `1` | Bevel segments. |
| `angle_limit` | number | 0 to 180 | `30` | Bevel only edges sharper than this angle in degrees. |
| `levels` | integer | 0 to 6 | `1` | Viewport subdivision levels. |
| `render_levels` | integer | 0 to 6 | `2` | Render subdivision levels. |
| `thickness` | number | -100 to 100 | `0.01` | Solidify thickness. |
| `offset` | number | -1 to 1 | `-1` | Solidify offset (-1 inside, 1 outside). |
| `count` | integer | 1 to 1000 | `2` | Array copies. |
| `relative_offset` | [x, y, z] | each -1000 to 1000 | `[1, 0, 0]` | Array offset relative to the object size. |
| `constant_offset` | [x, y, z] | each -100000 to 100000 |  | Array offset in metres. |
| `axes` | list of x/y/z |  | `['x']` | Mirror axes, a list of 'x', 'y', 'z'. |
| `clip` | boolean |  | `false` | Mirror clipping. |

### `add_light`

Add a light. energy is watts for point, spot and area, W/m2 for sun.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `type` (required) | choice | `point`, `sun`, `spot`, `area` |  | Light type. |
| `name` | name | up to 63 characters |  | Object name; must not already exist. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `energy` | number | 0 to 1e+07 |  | Power; defaults per type (point 1000, sun 3, spot 1000, area 500). |
| `color` | color |  | `[1, 1, 1, 1]` | Light colour. |
| `size` | number | 0 to 1000 |  | Point/spot radius or area edge length in metres; sun angular diameter in degrees. |
| `spot_angle` | number | 1 to 180 | `45` | Spot cone angle in degrees. |
| `spot_blend` | number | 0 to 1 | `0.15` | Spot edge softness. |

### `add_camera`

Add a camera and (by default) make it the active one.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `name` | name | up to 63 characters |  | Object name; must not already exist. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `lens` | number | 1 to 5000 | `50` | Focal length in millimetres. |
| `sensor_width` | number | 1 to 100 | `36` | Sensor width in millimetres. |
| `projection` | choice | `perspective`, `orthographic` | `perspective` | Projection. |
| `ortho_scale` | number | 0.001 to 100000 | `6.0` | Orthographic scale. |
| `dof_focus_object` | name | up to 63 characters |  | Object to keep in focus. |
| `dof_focus_distance` | number | 0.001 to 100000 |  | Focus distance in metres. |
| `f_stop` | number | 0.1 to 128 |  | Aperture f-stop; enables depth of field. |
| `make_active` | boolean |  | `true` | Make this the scene camera. |

### `track_to`

Make an object always point at another one (a Track To constraint).

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `object` (required) | name | up to 63 characters |  | Object that turns (usually a camera or light). |
| `target` (required) | name | up to 63 characters |  | Object to look at. |

### `add_text`

Add 3D text.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `body` (required) | text | up to 500 characters |  | The text. |
| `name` | name | up to 63 characters |  | Object name; must not already exist. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `scale` | [x, y, z] | each -10000 to 10000 | `[1, 1, 1]` | Scale [x, y, z]. |
| `size` | number | 0.001 to 1000 | `1.0` | Letter size in metres. |
| `extrude` | number | 0 to 100 | `0.0` | Extrusion depth in metres. |
| `material` | name | up to 63 characters |  | Name of a material created earlier. |

### `set_render`

Configure the renderer. The engine name is mapped to the right internal identifier for the Blender version.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `engine` | choice | `eevee`, `cycles`, `workbench` |  | Render engine. |
| `resolution` | [w, h] | each 1 to 16384 | `[1920, 1080]` | [width, height] in pixels. |
| `percentage` | integer | 1 to 1000 | `100` | Resolution percentage. |
| `samples` | integer | 1 to 65536 |  | Samples per pixel. |
| `denoise` | boolean |  |  | Denoise (Cycles). |
| `device` | choice | `cpu`, `gpu` | `cpu` | Cycles device; GPU falls back to CPU when none is usable. |
| `transparent` | boolean |  | `false` | Transparent film background. |
| `view_transform` | choice | `standard`, `filmic`, `agx`, `raw`, `neutral` |  | Colour view transform (falls back with a warning when the version lacks it). |

At least one of `engine`, `resolution`, `percentage`, `samples`, `denoise`, `device`, `transparent`, `view_transform` is required.

### `render`

Render the still image from the active camera and write it.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `path` (required) | path | ends in `.png`, `.jpg`, `.jpeg` |  | Output .png or .jpg file. |
| `quality` | integer | 1 to 100 | `90` | JPEG quality. |

### `compositor_glare`

Add a glare / bloom effect through compositor nodes.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `glare_type` | choice | `bloom`, `fog_glow`, `streaks`, `ghosts`, `simple_star` | `fog_glow` | Glare type (bloom needs Blender 4.2+; older versions use fog_glow). |
| `threshold` | number | 0 to 1000 | `1.0` | Brightness threshold. |
| `mix` | number | -1 to 1 | `0.0` | Mix between original (-1) and glare (1). |
| `size` | integer | 1 to 9 | `8` | Glare size. |
| `quality` | choice | `low`, `medium`, `high` | `high` | Quality. |

### `import_model`

Import a model file from the project folder into the scene.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `path` (required) | path | ends in `.stl`, `.obj`, `.fbx`, `.gltf`, `.glb`, `.ply` |  | Model file (.stl, .obj, .fbx, .gltf, .glb, .ply). |
| `format` | choice | `stl`, `obj`, `fbx`, `gltf`, `ply` |  | File format; inferred from the extension when omitted. |
| `location` | [x, y, z] | each -100000 to 100000 | `[0, 0, 0]` | World position in metres [x, y, z]. |
| `rotation` | [x, y, z] | each -36000 to 36000 | `[0, 0, 0]` | Euler rotation in degrees [x, y, z]. |
| `scale` | [x, y, z] | each -10000 to 10000 | `[1, 1, 1]` | Scale [x, y, z]. |

### `export_model`

Export objects to a model file inside the project folder.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `path` (required) | path | ends in `.stl`, `.obj`, `.fbx`, `.gltf`, `.glb` |  | Destination (.stl, .obj, .fbx, .gltf, .glb). |
| `format` | choice | `stl`, `obj`, `fbx`, `gltf` |  | File format; inferred from the extension when omitted. |
| `target` | glob | up to 100 characters |  | Glob of object names to export (default: all meshes). |
| `apply_modifiers` | boolean |  | `true` | Apply modifiers in the export. |

### `assert`

Check the scene and report observed versus expected. A failed assertion counts as an error.

| Field | Type | Values | Default | Meaning |
| --- | --- | --- | --- | --- |
| `kind` (required) | choice | `object_count`, `object_exists`, `material_assigned`, `resolution`, `engine`, `camera_active`, `bbox_within`, `file_exists` |  | What to check. |
| `label` | text | up to 200 characters |  | Name shown in the report. |
| `cmp` | choice | `eq`, `ge`, `le` | `eq` | Comparison for object_count. |
| `count` | integer | 0 to 1e+06 |  | Expected object count. |
| `object_type` | choice | `mesh`, `light`, `camera`, `empty`, `text`, `curve`, `armature`, `surface`, `lattice`, `grease_pencil` |  | Count only this object type. |
| `name` | glob | up to 100 characters |  | Object name or glob. |
| `object` | name | up to 63 characters |  | Object name. |
| `material` | name | up to 63 characters |  | Material name. |
| `width` | integer | 1 to 16384 |  | Expected resolution width. |
| `height` | integer | 1 to 16384 |  | Expected resolution height. |
| `engine` | choice | `eevee`, `cycles`, `workbench` |  | Expected engine. |
| `min` | [x, y, z] | each -100000 to 100000 |  | Lower corner of the allowed world box. |
| `max` | [x, y, z] | each -100000 to 100000 |  | Upper corner of the allowed world box. |
| `tolerance` | number | 0 to 1000 | `0.0001` | Slack on each side of the box. |
| `path` | path |  |  | File to check. |
| `min_bytes` | integer | 0 to 1.09951e+12 | `1` | Minimum file size. |

## Notes on the ops that depend on the Blender version

**Render engine names.** `set_render.engine` takes `eevee`, `cycles` or
`workbench` and maps it to the internal identifier of the running Blender:

| Blender | `eevee` | `cycles` | `workbench` |
| --- | --- | --- | --- |
| before 4.2 (3.6, 4.0, 4.1) | `BLENDER_EEVEE` | `CYCLES` | `BLENDER_WORKBENCH` |
| 4.2 to 4.5 | `BLENDER_EEVEE_NEXT` | `CYCLES` | `BLENDER_WORKBENCH` |
| 5.0 and later | `BLENDER_EEVEE` | `CYCLES` | `BLENDER_WORKBENCH` |

The `engine` assertion compares logical names, so it holds on every version.

**Samples.** On Cycles `samples` is the sample count, on EEVEE the render
samples, on Workbench the nearest value of its anti-aliasing list (off, FXAA, 5,
8, 11, 16, 32). `denoise` applies to Cycles only (other engines ignore it and a
warning says so). `device: "gpu"` enables the first usable compute device (OptiX,
CUDA, HIP, Metal, oneAPI) and falls back to the CPU with a warning when there is
none.

**View transform.** `standard`, `filmic`, `agx`, `raw` and `neutral` are mapped to
what the version offers (`neutral` is "Khronos PBR Neutral", Blender 4.2+). When
the exact one is missing the nearest available is used and a warning names it.

**Glare.** `bloom` needs Blender 4.2 or later; on older versions `fog_glow` is used
with a warning. The effect is built from compositor nodes (a render layer, a
glare node and the output), replacing any compositor tree already in the scene.

**Workbench.** The Workbench engine ignores node materials, so `add_material` also
sets the material's viewport colour, metallic and roughness; a Workbench render
shows the colours. It is the fastest engine and needs no GPU, so it is what
the smoke scene uses by default. EEVEE and Cycles work in the same container but
are slower on software OpenGL.

**Import and export.** The importer used depends on the version (`wm.stl_import`
from 4.0, `import_mesh.stl` before; the same for OBJ and PLY). `location`,
`rotation` and `scale` of `import_model` are applied to the imported top-level
objects. `export_model` writes the objects matching `target` (a glob; all meshes
by default) with their modifiers applied; `.glb` is a binary glTF, `.gltf` a
single self-contained file.

The test suite runs the real Blender 4.3.2. The paths written for other versions
(the 3.6 operators, the 5.x engine names and compositor node group) follow the
API of those versions but are covered only by the unit tests of the name
mapping.

## Verification

`assert` ops are ordinary ops: they run where they are written, report
**observed** and **expected**, and a failed assertion counts as an error (the run
exits non-zero, and with `stop_on_error` it stops). Put them last.

| `kind` | Fields | Passes when |
| --- | --- | --- |
| `object_count` | `count`, `object_type`?, `cmp`? (`eq`, `ge`, `le`) | the number of objects (of that type) compares as asked |
| `object_exists` | `name` (glob) | an object with that name exists |
| `material_assigned` | `object`, `material` | the object has that material in a slot |
| `resolution` | `width`, `height` | `render.resolution_x/y` equal them |
| `engine` | `engine` | the scene's engine is that one |
| `camera_active` | `name`? (glob) | the scene has an active camera (with that name) |
| `bbox_within` | `object`, `min`?, `max`?, `tolerance`? | the object's world bounding box (with modifiers) is inside the box |
| `file_exists` | `path`, `min_bytes`? | the file exists inside the project folder and is at least that big |

To verify a saved file, run a second scene that starts with
`{"op": "load_blend", "path": "model.blend"}` followed by assertions; the smoke
action does exactly this.

## The report

The runner prints exactly one line, `BLENDER_SCENE_REPORT {json}`, at the end,
and exits 0 only when nothing failed (exit code 1 when an op or assertion failed,
3 for an uncaught Python error in the runner itself):

```json
{
  "schema": 1,
  "blender": {"version": "4.3.2"},
  "ok": true, "errors": 0, "duration_s": 1.24, "name": "smoke scene",
  "ops": [
    {"index": 0, "op": "new_scene", "status": "ok", "duration_s": 0.047},
    {"index": 3, "op": "add_mesh", "status": "ok", "duration_s": 0.001, "info": {"object": "Ground", "vertices": 4}},
    {"index": 11, "op": "render", "status": "ok", "duration_s": 0.469, "info": {"bytes": 58869, "resolution": [320, 240], "engine": "BLENDER_WORKBENCH"}},
    {"index": 14, "op": "assert", "status": "error", "duration_s": 0.0, "message": "assertion failed (object_count): 5 object(s), expected eq 6"}
  ],
  "assertions": [
    {"index": 14, "kind": "object_count", "label": "object_count", "passed": false, "observed": 5, "expected": "eq 6", "message": "5 object(s), expected eq 6"}
  ],
  "outputs": [
    {"path": "/data/blender_scenes/s1/smoke.png", "bytes": 58869, "kind": "render"},
    {"path": "/data/blender_scenes/s1/smoke.blend", "bytes": 501056, "kind": "blend"}
  ],
  "warnings": []
}
```

`status` is `ok`, `error` or `skipped`. `outputs[].kind` is `render`, `blend` or
`model`.

## Running it

`run_scene(scene, root, timeout=None, ...)` (`src/blender_scene/run.py`):

1. validates against `root` (stage `validate` on failure; Blender is never
   started);
2. finds Blender (stage `discovery` when there is none);
3. writes the normalised scene to `<root>/.blender_scene/scene.json` and starts
   `blender --background --factory-startup --disable-autoexec -noaudio --python-exit-code 3 --python blender_runner.py -- <scene.json> <root>` with the
   project root as working directory, `PYTHONHOME`/`PYTHONPATH` removed from the
   environment, no console window on Windows and a timeout;
4. parses the report line from the output.

It never raises. The result has `ok`, a `stage` (`validate`, `discovery`,
`launch`, `timeout`, `no_report` or `done`), `errors` (one line per problem,
written for the model), `warnings`, `outputs`, `images` (the rendered PNG/JPEG
paths), `assertions` (`total` and `failed`), the parsed `report`, `returncode`,
`duration_s` and, on failure, `stdout_tail` / `stderr_tail`. When Blender exits
without a report line the result says so and carries the end of its error
output; when it exceeds the timeout it is stopped and the partial output is
kept.

The timeout is 300 seconds by default: the `blender_timeout_seconds` setting,
or the `timeout` argument of the call, clamped to 5-3600.

Blender is started with `--disable-autoexec`, so scripts and drivers inside a
loaded `.blend` do not run. Saving does not leave `.blend1` backups.

## Finding Blender

`probe()` returns the first Blender found, its version (from `blender --version`)
and how it was found, or a message that says Blender is not installed and how to
point Faustus to it. Order:

1. the `blender_path` setting (the executable, its folder, or a macOS `.app`);
2. the `BLENDER_PATH` environment variable (same forms);
3. `blender` on `PATH`;
4. on Windows, `Blender Foundation\Blender <version>\blender.exe` under
   `%ProgramFiles%`, `C:\Program Files` and `%LOCALAPPDATA%\Programs`, **newest
   version first** (5.0, 4.5, 4.3, 4.0, 3.6 and anything else found in those
   folders);
5. elsewhere, `/usr/bin`, `/usr/local/bin`, `/snap/bin`, the Flatpak export, and
   any `blender*` folder under `/opt`, `/usr/local`, `/Applications` or the home
   folder (newest version first), plus
   `/Applications/Blender.app/Contents/MacOS/Blender`.

A setting or variable that points nowhere does not stop the search; `probe()`
lists it under `notes`.

## The smoke scene

`smoke_scene(engine="workbench")` builds 13 ops and 9 assertions: a clean scene,
a world colour, a material, a ground plane, a beveled cube with that material, a
smooth sphere, a sun, a camera that tracks the cube, a 320x240 render at 8
samples, a PNG and a `.blend` saved next to each other. The assertions cover the
mesh and object counts, the cube's name, its material, the resolution, the
active camera, the cube's bounding box and both files.
`reload_scene()` re-opens the saved `.blend` in a new Blender and repeats the
assertions (plus the engine). On Blender 4.3.2 without a GPU the whole smoke run
takes about 1.3 seconds and the PNG is about 59 KB.

## The `blender_scene` agent tool

Arguments: `action` (`probe`, `schema`, `validate`, `run`, `smoke`; `run` when a
scene is given), `scene` (an object with `ops`) or `ops` and `meta` directly,
`folder`, `timeout`, `format` and `op` (for `schema`), `engine` (for `smoke`).

| Action | What it does |
| --- | --- |
| `probe` | where Blender is and which version, or that it is not installed |
| `schema` | the format as compact text (`format: "json_schema"` for the full JSON Schema; `op` for one op) |
| `validate` | validation only, without Blender |
| `run` | validate, run, return the report summary, the files written and the rendered image |
| `smoke` | run the smoke scene, then the reload check |

Files are written only inside `<data dir>/blender_scenes/<session id>/`, and
inside `<folder>` below it when a `folder` is given (one folder per model keeps
files apart; a folder name is reduced to letters, digits, `.`, `_` and `-`).
Paths in the output are shown as `${PROJECT_ROOT}/...`, ready to be reused in a
later scene, so a later `run` can `load_blend` or `import_model` something an
earlier one wrote.

A rendered PNG or JPEG is returned in `images` (the model sees it when it can
see images, and the chat shows it under the tool call), and the last one is also
copied next to the generated images and added to the gallery (`image_url`,
`image_id`), the same way other generated images are. Nothing is attached above
8 MB.

A run whose ops failed returns `exit_code: 1` with its full output, report and
images (like a command that exits non-zero); a scene that did not run at all
(invalid, no Blender, timeout) returns an `error`.

Permission class: the tool reads files in the project folder and writes new ones
inside it (`read_workspace` and `write_private`), so the permission gate treats
it like the other tools that write generated files into private storage. It is
refused to non-admin users, is not available in plan mode (`run` writes) and is
listed in the compact tool catalogue rather than carrying its schema on every
turn.

## The `blender_scene` MCP server

`mcp_servers/blender_scene_server.py`, registered as a built-in server
(`src/builtin_mcp.py`) and treated as a twin of the native tool.

| Tool | Does |
| --- | --- |
| `blender_probe` | Blender path and version |
| `blender_scene_schema` | the format (`format`: `summary` or `json_schema`; `op`) |
| `blender_scene_validate` | validation without Blender |
| `blender_scene_run` | validate and run; the last render is returned as an image |
| `blender_smoke` | the smoke scene and the reload check |

It writes only under `<data dir>/blender_scenes/mcp[/<folder>]`, never touches
the gallery, and returns errors as messages.

## REST routes

All require an administrator. A run writes under
`<data dir>/blender_scenes/api[/<folder>]`.

| Route | Body / query | Returns |
| --- | --- | --- |
| `GET /api/blender/probe` | | the probe result |
| `GET /api/blender/schema` | `format=summary` or `json_schema`, `op` | the summary, or the JSON Schema |
| `POST /api/blender/validate` | `{scene}` or `{ops, meta?}`, `folder?` | `{ok, ops, errors, error_lines, warnings}` |
| `POST /api/blender/run` | `{scene}` or `{ops, meta?}`, `folder?`, `timeout?` | the `run_scene` result |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `blender_path` | `""` | Blender executable or its folder; empty searches `BLENDER_PATH`, `PATH` and the install folders |
| `blender_timeout_seconds` | `300` | how long one scene run may take (5-3600) |

## Safety

- **No free code.** The format has no op that evaluates a script, and the runner
  contains no `exec`, `eval` or subprocess call (a test checks the source).
- **Typed and bounded.** Every field has a type and a range; a scene is limited to
  400 ops and 50 million pixels.
- **Paths confined** to the project folder in the validator and again in the
  runner, including symbolic links, `..`, other drives and UNC shares.
- **Timeout.** Blender is stopped when it exceeds the limit.
- **Autoexec off.** Scripts embedded in a loaded `.blend` do not run.
- **No network needed.** The runner never opens a connection; Blender starts
  with the factory settings, so user add-ons and preferences are not loaded.
- **Private by default.** The files live in the data directory, per session; the
  tool is admin-only.

## Tests

- `tests/test_blender_scene.py`: validator, path rules (POSIX and Windows), engine
  names per version, report parsing, discovery order, launcher with a fake Blender.
- `tests/test_blender_scene_tool.py`: registration in the catalogues, every tool
  action, the MCP server and the REST routes.
- `tests/test_blender_scene_integration.py`: runs the real Blender when it is
  installed (skipped otherwise): the smoke scene renders a PNG and saves a
  `.blend` with all assertions passing, a second run re-loads the `.blend` and
  asserts again, failing ops and assertions are reported, imports and exports
  round-trip, a Cycles render with glare writes a JPEG and the runner refuses
  paths outside the folder.
