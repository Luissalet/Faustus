"""
workflows/schema_check.py — the small slice of JSON Schema a workflow node
needs to check what a model handed back.

A model node that promises structured output has to be able to *refuse* what
it got, deterministically and with a message the repair prompt can use. That
does not need a full validator, and the project does not declare one as a
dependency, so this is the subset that matters for "extract these fields" and
"answer in this shape":

    type (one name or a list), enum, const, properties, required,
    additionalProperties (true/false), items, minItems, maxItems,
    minimum, maximum, exclusiveMinimum, exclusiveMaximum,
    minLength, maxLength, pattern, anyOf, oneOf, allOf

A schema that uses anything else (`$ref`, `patternProperties`, `if/then`…) is
reported by :func:`unsupported_keywords` so the node can refuse the
*definition* instead of quietly checking less than the author wrote — a
validator that ignores a keyword is a validator that says "valid" for the
wrong reason.

Pure: no I/O, no model, no clock.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

__all__ = ["SUPPORTED_KEYWORDS", "unsupported_keywords", "validate", "extract_json",
           "schema_problems"]

SUPPORTED_KEYWORDS = frozenset({
    "type", "enum", "const", "properties", "required", "additionalProperties",
    "items", "minItems", "maxItems", "minimum", "maximum", "exclusiveMinimum",
    "exclusiveMaximum", "minLength", "maxLength", "pattern", "anyOf", "oneOf",
    "allOf",
    # Annotations: read by people, never by the check.
    "title", "description", "default", "examples", "$schema", "$id",
})

_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_MAX_DEPTH = 16


def _type_ok(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or \
               (isinstance(value, float) and value.is_integer())
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def unsupported_keywords(schema: Any, _path: str = "$", _depth: int = 0) -> List[str]:
    """Every keyword in `schema` this checker would not enforce, as
    `path: keyword` lines. Empty means the whole schema is honoured."""
    out: List[str] = []
    if _depth > _MAX_DEPTH:
        return [f"{_path}: nested deeper than {_MAX_DEPTH} levels"]
    if not isinstance(schema, Mapping):
        return out
    for key, value in schema.items():
        if key not in SUPPORTED_KEYWORDS:
            out.append(f"{_path}: {key}")
            continue
        if key == "properties" and isinstance(value, Mapping):
            for name, sub in value.items():
                out.extend(unsupported_keywords(sub, f"{_path}.{name}", _depth + 1))
        elif key == "items":
            out.extend(unsupported_keywords(value, f"{_path}[]", _depth + 1))
        elif key == "additionalProperties" and isinstance(value, Mapping):
            out.extend(unsupported_keywords(value, f"{_path}.*", _depth + 1))
        elif key in ("anyOf", "oneOf", "allOf") and isinstance(value, (list, tuple)):
            for i, sub in enumerate(value):
                out.extend(unsupported_keywords(sub, f"{_path}.{key}[{i}]", _depth + 1))
    return out


def schema_problems(schema: Any) -> List[str]:
    """Why `schema` cannot be used as a node's output schema, or `[]`."""
    if not isinstance(schema, Mapping) or not schema:
        return ["the schema must be a non-empty JSON object"]
    problems = [f"unsupported keyword {line}" for line in unsupported_keywords(schema)]
    declared = schema.get("type")
    names = [declared] if isinstance(declared, str) else declared
    if names is not None:
        if not isinstance(names, (list, tuple)) or any(n not in _TYPES for n in names):
            problems.append(f"`type` must be one of {sorted(_TYPES)} (or a list of them)")
    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"`pattern` is not a valid regular expression ({exc})")
    return problems


def validate(value: Any, schema: Mapping[str, Any], _path: str = "$", _depth: int = 0) -> List[str]:
    """Every way `value` breaks `schema`, as `path: message` lines. Empty
    means valid. The messages are written to be pasted into a repair prompt."""
    errors: List[str] = []
    if _depth > _MAX_DEPTH or not isinstance(schema, Mapping):
        return errors

    declared = schema.get("type")
    if declared is not None:
        names = [declared] if isinstance(declared, str) else list(declared)
        if not any(_type_ok(value, n) for n in names):
            errors.append(f"{_path}: expected {' or '.join(names)}, got {_kind(value)}")
            return errors                     # the rest would only repeat the mistake

    if "const" in schema and value != schema["const"]:
        errors.append(f"{_path}: must equal {json.dumps(schema['const'], ensure_ascii=False)}")
    if "enum" in schema and isinstance(schema["enum"], (list, tuple)) and value not in schema["enum"]:
        errors.append(f"{_path}: must be one of {json.dumps(list(schema['enum']), ensure_ascii=False)}")

    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), Mapping) else {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{_path}: missing required field {name!r}")
        for name, sub in props.items():
            if name in value:
                errors.extend(validate(value[name], sub, f"{_path}.{name}", _depth + 1))
        extra = schema.get("additionalProperties")
        unknown = [k for k in value if k not in props]
        if extra is False and unknown:
            errors.append(f"{_path}: unexpected field(s) {sorted(unknown)}")
        elif isinstance(extra, Mapping):
            for name in unknown:
                errors.extend(validate(value[name], extra, f"{_path}.{name}", _depth + 1))

    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            errors.append(f"{_path}: needs at least {schema['minItems']} item(s), has {len(value)}")
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            errors.append(f"{_path}: allows at most {schema['maxItems']} item(s), has {len(value)}")
        if isinstance(schema.get("items"), Mapping):
            for i, item in enumerate(value):
                errors.extend(validate(item, schema["items"], f"{_path}[{i}]", _depth + 1))

    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            errors.append(f"{_path}: shorter than {schema['minLength']} character(s)")
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            errors.append(f"{_path}: longer than {schema['maxLength']} character(s)")
        if isinstance(schema.get("pattern"), str):
            try:
                if re.search(schema["pattern"], value) is None:
                    errors.append(f"{_path}: does not match the pattern {schema['pattern']!r}")
            except re.error:
                errors.append(f"{_path}: the schema's pattern is not a valid regular expression")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, bad in (("minimum", lambda v, b: v < b), ("maximum", lambda v, b: v > b),
                         ("exclusiveMinimum", lambda v, b: v <= b),
                         ("exclusiveMaximum", lambda v, b: v >= b)):
            bound = schema.get(key)
            if isinstance(bound, (int, float)) and not isinstance(bound, bool) and bad(value, bound):
                errors.append(f"{_path}: {key} is {bound}, got {value}")

    for sub in schema.get("allOf") or []:
        errors.extend(validate(value, sub, _path, _depth + 1))
    any_of = schema.get("anyOf")
    if isinstance(any_of, (list, tuple)) and any_of:
        if not any(not validate(value, sub, _path, _depth + 1) for sub in any_of):
            errors.append(f"{_path}: does not match any of the allowed shapes")
    one_of = schema.get("oneOf")
    if isinstance(one_of, (list, tuple)) and one_of:
        matches = sum(1 for sub in one_of if not validate(value, sub, _path, _depth + 1))
        if matches != 1:
            errors.append(f"{_path}: must match exactly one of the allowed shapes (matches {matches})")
    return errors


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json(text: Any) -> Tuple[Optional[Any], str]:
    """`(value, "")` for the first JSON object or array in a model reply, or
    `(None, reason)`. Models wrap JSON in fences and prose; both are
    tolerated, because refusing a correct answer for its packaging is how a
    repair retry gets spent on nothing. Nothing is guessed: the candidate has
    to parse as JSON exactly."""
    raw = str(text or "").strip()
    if not raw:
        return None, "the reply was empty"
    candidates: List[str] = [raw]
    candidates.extend(m.group(1).strip() for m in _FENCE.finditer(raw))
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if 0 <= start < end:
            candidates.append(raw[start:end + 1])
    last = "no JSON value found in the reply"
    for candidate in candidates:
        try:
            return json.loads(candidate), ""
        except (ValueError, TypeError) as exc:
            last = f"the reply is not valid JSON ({exc})"
    return None, last
