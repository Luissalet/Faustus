"""The browser relay's small, shared wire contract. No browser execution here."""
from __future__ import annotations

from copy import deepcopy

EXTENSION_ID = "cneonpjikbdhbaddfpmmdjemfdphlppa"
ORIGIN = "chrome-extension://" + EXTENSION_ID
READ_ACTIONS = frozenset({"tabs", "snapshot", "screenshot"})
ACTIONS = (*sorted(READ_ACTIONS), "click", "type", "press", "navigate", "release", "upload", "download")
SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "tab_id": {"type": "integer", "minimum": 0},
        "ref": {"type": "string", "minLength": 1},
        "text": {"type": "string"},
        "files": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
        "output_path": {"type": "string", "minLength": 1},
        "key": {"type": "string", "minLength": 1},
        "url": {"type": "string", "minLength": 1},
        "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
        "view": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "roles": {"type": "array", "items": {"type": "string"},
                          "description": "Exact case-sensitive AX roles: StaticText or button, not statictext or Button.",
                          "examples": [["StaticText"], ["button"]]},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1},
                "context": {"type": "boolean", "default": False,
                            "description": "Add original direct parents and siblings in context_nodes; nodes stays flat."},
            },
            "additionalProperties": False,
        },
    },
    "required": ["action"], "additionalProperties": False,
    "allOf": [
        {"if": {"properties": {"action": {"const": action}}},
         "then": {"required": required}}
        for action, required in [
            ("snapshot", ["tab_id"]), ("screenshot", ["tab_id"]),
            ("click", ["tab_id", "ref"]), ("type", ["tab_id", "ref", "text"]),
            ("press", ["tab_id", "key"]), ("navigate", ["tab_id", "url"]),
            ("release", ["tab_id"]),
            ("upload", ["tab_id", "ref", "files"]),
            ("download", ["tab_id", "ref", "output_path"]),
        ]
    ] + [{"if": {"required": ["view"]},
          "then": {"properties": {"action": {"enum": ["snapshot", "click", "type", "press", "navigate", "upload"]}}}},
        {"if": {"required": ["files"]}, "then": {"properties": {"action": {"const": "upload"}}}},
        {"if": {"required": ["output_path"]}, "then": {"properties": {"action": {"const": "download"}}}},
    ],
}


def failure(code: str, message: str, phase: str = "not_dispatched") -> dict:
    return {"ok": False, "error": {"code": code, "message": message}, "phase": phase}


def validate_arguments(arguments: object) -> dict:
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(SCHEMA).iter_errors(arguments))
    if errors:
        raise ValueError(errors[0].message)
    # Presentation options include a nested roles array; detach before any await.
    return deepcopy(arguments)


def validate_result(value: object) -> dict:
    if not isinstance(value, dict) or value.get("type") != "result":
        raise ValueError("Expected a result message")
    if not isinstance(value.get("id"), str) or type(value.get("ok")) is not bool:
        raise ValueError("Result requires a string id and boolean ok")
    if value["ok"]:
        if value.get("phase") not in {"readback_complete", "action_confirmed"}:
            raise ValueError("Invalid success phase")
        if not isinstance(value.get("result"), dict):
            raise ValueError("Result payload must be an object")
    else:
        error = value.get("error")
        if not isinstance(error, dict) or not all(isinstance(error.get(k), str) and error[k] for k in ("code", "message")):
            raise ValueError("Error requires code and message")
        if value.get("phase") not in {"not_dispatched", "outcome_unknown", "action_confirmed"}:
            raise ValueError("Invalid error phase")
    return {k: value[k] for k in ("ok", "phase", "result" if value["ok"] else "error")}
