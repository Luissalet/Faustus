"""The scene format as compact text: what the agent tool shows the model.

`scene_summary()` is a few thousand characters (the full JSON Schema is several
times larger); it is generated from the same table as the validator.
"""
from __future__ import annotations

from typing import Optional

from .spec import OPS, Field

_SHORT = {"vec3": "[x,y,z]", "vec2i": "[w,h]", "color": "color", "axes": "[axes]",
          "bool": "bool", "path": "path", "name": "name", "pattern": "glob", "str": "text"}

HEADER = (
    "Scene = {\"meta\": {name, description, stop_on_error}, \"ops\": [ {\"op\": \"<name>\", ...fields}, ... ]} "
    "(a bare list of ops also works). Ops run in order inside Blender; a failing op is reported and the run "
    "continues unless meta.stop_on_error. A run starts from an empty scene. A field marked * is required. "
    "Vectors are [x,y,z] (rotation in degrees, locations in metres). color = [r,g,b(,a)] linear 0-1 or "
    "\"#rrggbb\" (sRGB). Every string may use ${PROJECT_ROOT}; a path field is relative to the project folder "
    "or starts with ${PROJECT_ROOT}/ and must stay inside it (no '..'). Objects and materials are referred to "
    "by name; create a material before using it. Put assert ops last."
)


def _field_text(name: str, f: Field) -> str:
    if f.kind == "enum":
        t = "|".join(f.enum)
    elif f.kind in ("num", "int"):
        t = "int" if f.kind == "int" else "num"
        if f.min is not None and f.max is not None:
            t += f"[{f.min:g}..{f.max:g}]"
    else:
        t = _SHORT.get(f.kind, f.kind)
    text = f"{name}{'*' if f.required else ''}:{t}"
    if f.default is not None and f.kind not in ("vec3", "color", "axes", "vec2i"):
        text += f"={f.default}"
    return text


def scene_summary(op: Optional[str] = None) -> str:
    """One block per op (or just ``op``), preceded by the format header."""
    names = [op] if op else list(OPS)
    lines = [] if op else [HEADER, ""]
    for name in names:
        spec = OPS[name]
        fields = "; ".join(_field_text(n, f) for n, f in spec.fields.items() if n != "note")
        lines.append(f"- {name}: {spec.doc}")
        if fields:
            lines.append(f"    {fields}")
        if spec.at_least_one_of:
            lines.append(f"    (at least one of: {', '.join(spec.at_least_one_of)})")
    return "\n".join(lines)
