"""Validate a typed scene before Blender is started.

`validate_scene` accepts the document as a dict, a bare list of ops or a JSON
string, and returns every problem it can find (not just the first) as precise
issues: op index, op name, field and reason, worded to be sent back to the
model. On success it also returns the *normalised* scene: strings with
``${PROJECT_ROOT}`` replaced, enums lower-cased, colours as ``[r, g, b, a]``,
numbers as floats and every path made absolute and confined to the folder.
The runner receives that normalised form and confines paths a second time.
"""
from __future__ import annotations

import difflib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import scene_common as common
from .spec import META_FIELDS, MAX_OPS, OPS, Field

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


@dataclass
class Issue:
    message: str
    op_index: Optional[int] = None
    op: Optional[str] = None
    field: Optional[str] = None

    def text(self) -> str:
        where = "scene" if self.op_index is None and self.op is None else (
            "meta" if self.op == "meta" else f"ops[{self.op_index}]")
        if self.op and self.op != "meta":
            where += f" ({self.op})"
        if self.field:
            where += f".{self.field}"
        return f"{where}: {self.message}"

    def to_dict(self) -> dict:
        return {"op_index": self.op_index, "op": self.op, "field": self.field, "message": self.message}


@dataclass
class ValidationResult:
    errors: List[Issue] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    scene: Optional[dict] = None     # normalised; only set when there are no errors
    op_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def error_lines(self) -> List[str]:
        return [e.text() for e in self.errors]

    def to_dict(self) -> dict:
        return {"ok": self.ok, "ops": self.op_count,
                "errors": [e.to_dict() for e in self.errors],
                "error_lines": self.error_lines(), "warnings": list(self.warnings)}


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _range_text(f: Field) -> str:
    if f.min is not None and f.max is not None:
        return f"between {f.min:g} and {f.max:g}"
    if f.min is not None:
        return f"at least {f.min:g}"
    if f.max is not None:
        return f"at most {f.max:g}"
    return ""


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _parse_hex(text: str) -> List[float]:
    h = text[1:]
    if len(h) in (3, 4):
        h = "".join(ch * 2 for ch in h)
    vals = [int(h[i:i + 2], 16) / 255.0 for i in range(0, len(h), 2)]
    rgb = [_srgb_to_linear(v) for v in vals[:3]]
    alpha = vals[3] if len(vals) == 4 else 1.0
    return [round(v, 6) for v in rgb] + [alpha]


def _check_field(f: Field, value: Any, root: str) -> Tuple[Any, Optional[str]]:
    """Return ``(normalised value, None)`` or ``(None, reason)``."""
    kind = f.kind
    if kind in ("name", "str", "pattern"):
        if not isinstance(value, str):
            return None, f"expected a string, got {_short(value)}"
        text = common.substitute(value, root)
        if not text.strip():
            return None, "must not be empty"
        if len(text) > f.max_len:
            return None, f"too long ({len(text)} characters, maximum {f.max_len})"
        if kind != "str" and _CONTROL.search(text):
            return None, "must not contain control characters"
        return text, None
    if kind == "path":
        if not isinstance(value, str):
            return None, f"expected a path string, got {_short(value)}"
        if len(value) > f.max_len:
            return None, f"path too long ({len(value)} characters, maximum {f.max_len})"
        resolved, err = common.confine_path(value, root)
        if err:
            return None, err
        if f.exts:
            low = resolved.lower()
            if not any(low.endswith(e) for e in f.exts):
                return None, f"unsupported file extension (expected one of: {', '.join(f.exts)})"
        return resolved, None
    if kind == "bool":
        if not isinstance(value, bool):
            return None, f"expected true or false, got {_short(value)}"
        return value, None
    if kind in ("num", "int"):
        if not _is_number(value):
            return None, f"expected a {'number' if kind == 'num' else 'whole number'}, got {_short(value)}"
        if kind == "int":
            if isinstance(value, float):
                if not value.is_integer():
                    return None, f"expected a whole number, got {value}"
                value = int(value)
        else:
            value = float(value)
        if (f.min is not None and value < f.min) or (f.max is not None and value > f.max):
            return None, f"out of range: {value} is not {_range_text(f)}"
        return value, None
    if kind == "enum":
        if not isinstance(value, str):
            return None, f"expected a string, got {_short(value)}"
        low = value.strip().lower()
        if low not in f.enum:
            close = difflib.get_close_matches(low, f.enum, n=1)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            return None, f"'{value}' is not allowed{hint}; expected one of: {', '.join(f.enum)}"
        return low, None
    if kind == "vec3":
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            n = len(value) if isinstance(value, (list, tuple)) else None
            return None, f"expected 3 numbers [x, y, z], got {('%d values' % n) if n is not None else _short(value)}"
        out = []
        for i, item in enumerate(value):
            if not _is_number(item):
                return None, f"element {i} must be a number, got {_short(item)}"
            if (f.min is not None and item < f.min) or (f.max is not None and item > f.max):
                return None, f"out of range: element {i} = {item} is not {_range_text(f)}"
            out.append(float(item))
        return out, None
    if kind == "vec2i":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None, f"expected 2 whole numbers [width, height], got {_short(value)}"
        out = []
        for i, item in enumerate(value):
            if not _is_number(item) or float(item) != int(item):
                return None, f"element {i} must be a whole number, got {_short(item)}"
            if not 1 <= int(item) <= 16384:
                return None, f"out of range: element {i} = {item} is not between 1 and 16384"
            out.append(int(item))
        return out, None
    if kind == "color":
        if isinstance(value, str):
            if not _HEX.match(value.strip()):
                return None, f"expected [r, g, b(, a)] in 0-1 or a '#rrggbb' string, got {_short(value)}"
            return _parse_hex(value.strip()), None
        if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
            return None, f"expected [r, g, b] or [r, g, b, a], got {_short(value)}"
        out = []
        for i, item in enumerate(value):
            if not _is_number(item):
                return None, f"element {i} must be a number, got {_short(item)}"
            if not 0 <= item <= 1:
                return None, f"out of range: element {i} = {item} is not between 0 and 1"
            out.append(float(item))
        if len(out) == 3:
            out.append(1.0)
        return out, None
    if kind == "axes":
        if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 3:
            return None, f"expected a list of 1 to 3 axes ('x', 'y', 'z'), got {_short(value)}"
        out = []
        for item in value:
            low = str(item).strip().lower() if isinstance(item, str) else None
            if low not in ("x", "y", "z"):
                return None, f"axis {_short(item)} is not one of 'x', 'y', 'z'"
            if low in out:
                return None, f"axis '{low}' is repeated"
            out.append(low)
        return out, None
    return None, f"unsupported field kind {kind}"  # pragma: no cover


def _suggest(name: str, choices) -> str:
    close = difflib.get_close_matches(str(name), list(choices), n=2)
    return f" (did you mean {' or '.join(repr(c) for c in close)}?)" if close else ""


def _validate_op(index: int, raw: Any, root: str, issues: List[Issue]) -> Optional[dict]:
    if not isinstance(raw, dict):
        issues.append(Issue(f"each op must be an object, got {_short(raw)}", index))
        return None
    name = raw.get("op")
    if not isinstance(name, str) or not name:
        issues.append(Issue("missing the 'op' field naming the operation", index))
        return None
    spec = OPS.get(name)
    if spec is None:
        issues.append(Issue(f"unknown op '{name}'{_suggest(name, OPS)}; valid ops: {', '.join(OPS)}", index, name))
        return None

    def add(fname: Optional[str], message: str) -> None:
        issues.append(Issue(message, index, name, fname))

    start = len(issues)
    norm: Dict[str, Any] = {"op": name}
    for key, value in raw.items():
        if key == "op":
            continue
        f = spec.fields.get(key)
        if f is None:
            add(key, f"unknown field{_suggest(key, spec.fields)}; allowed fields: "
                     + ", ".join(k for k in spec.fields if k != "note"))
            continue
        if key == "note":
            if not isinstance(value, str):
                add(key, f"expected a string, got {_short(value)}")
            continue
        result, err = _check_field(f, value, root)
        if err:
            add(key, err)
        else:
            norm[key] = result
    for fname, f in spec.fields.items():
        if f.required and fname not in raw:
            add(fname, "is required")
    if spec.at_least_one_of and not any(k in raw for k in spec.at_least_one_of):
        add(None, "give at least one of: " + ", ".join(spec.at_least_one_of))
    if spec.check is not None:
        spec.check(norm, lambda fname, msg: add(fname, msg), raw)
    return norm if len(issues) == start else None


def validate_scene(scene: Any, root: str) -> ValidationResult:
    """Validate ``scene`` (dict, list of ops or JSON text) against ``root``."""
    result = ValidationResult()
    if isinstance(scene, (str, bytes)):
        try:
            scene = json.loads(scene)
        except (ValueError, TypeError) as exc:
            result.errors.append(Issue(f"not valid JSON: {exc}"))
            return result
    if isinstance(scene, list):
        scene = {"ops": scene}
    if not isinstance(scene, dict):
        result.errors.append(Issue(f"the scene must be an object with 'ops' or a list of ops, got {_short(scene)}"))
        return result
    for key in scene:
        if key not in ("meta", "ops"):
            result.errors.append(Issue(f"unknown top-level field '{key}'{_suggest(key, ('meta', 'ops'))}; allowed: meta, ops"))
    meta_raw = scene.get("meta", {})
    meta: Dict[str, Any] = {}
    if not isinstance(meta_raw, dict):
        result.errors.append(Issue(f"'meta' must be an object, got {_short(meta_raw)}", None, "meta"))
    else:
        for key, value in meta_raw.items():
            f = META_FIELDS.get(key)
            if f is None:
                result.errors.append(Issue(f"unknown field{_suggest(key, META_FIELDS)}; allowed: "
                                           + ", ".join(META_FIELDS), None, "meta", key))
                continue
            val, err = _check_field(f, value, root)
            if err:
                result.errors.append(Issue(err, None, "meta", key))
            else:
                meta[key] = val
    ops_raw = scene.get("ops")
    if ops_raw is None:
        result.errors.append(Issue("missing 'ops': the list of operations"))
        return result
    if not isinstance(ops_raw, list) or not ops_raw:
        result.errors.append(Issue("'ops' must be a non-empty list"))
        return result
    if len(ops_raw) > MAX_OPS:
        result.errors.append(Issue(f"too many ops ({len(ops_raw)}); the maximum is {MAX_OPS}"))
        return result
    result.op_count = len(ops_raw)

    ops: List[dict] = []
    for index, raw in enumerate(ops_raw):
        norm = _validate_op(index, raw, root, result.errors)
        if norm is not None:
            ops.append(norm)

    # Static hints that never block a run.
    names = [o.get("op") for o in ops_raw if isinstance(o, dict)]
    for i, n in enumerate(names):
        if n == "render" and not any(m in ("add_camera", "load_blend", "import_model") for m in names[:i]):
            result.warnings.append(f"ops[{i}] render: no add_camera, load_blend or import_model before it; "
                                   "the render fails unless a camera exists")
        if n == "assert" and i < len(names) - 1 and "render" in names[i + 1:]:
            result.warnings.append(f"ops[{i}] assert runs before a later render; assertions are best placed at the end")
    if not result.errors:
        result.scene = {"meta": meta, "ops": ops}
    return result
