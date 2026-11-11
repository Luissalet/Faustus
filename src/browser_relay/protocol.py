"""The browser relay's small, shared wire contract. No browser execution here."""
from __future__ import annotations

from copy import deepcopy

EXTENSION_ID = "cneonpjikbdhbaddfpmmdjemfdphlppa"
ORIGIN = "chrome-extension://" + EXTENSION_ID
READ_ACTIONS = frozenset({"tabs", "snapshot", "screenshot", "wait", "sessions", "handoff_status"})
ACTIONS = (*sorted(READ_ACTIONS), "click", "type", "press", "navigate", "release", "upload", "download",
           "scroll", "hover", "select", "reload", "back", "forward", "open_tab", "close_tab", "reload_extension",
           "session_save", "session_open", "session_forget", "adopt_tab", "handoff",
           "har_start", "har_stop", "recording_start", "recording_stop", "last_minute")
READBACK_ACTIONS = ("snapshot", "click", "type", "press", "navigate", "upload", "scroll", "hover", "select", "reload", "back",
                    "forward", "wait")
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
        "name": {"type": "string", "minLength": 1, "description": "Durable browser session name; uses the existing browser profile."},
        "reason": {"type": "string", "description": "handoff/adopt_tab: explain the person's next step in the extension card."},
        "cursor": {"type": "boolean", "default": False, "description": "recording_start: show a cursor marker in this tab's video."},
        "include_bodies": {"type": "boolean", "default": False, "description": "har_start: include available response bodies in the local HAR."},
        "detail": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
        "direction": {"type": "string", "enum": ["up", "down", "left", "right"],
                      "description": "scroll: wheel direction; with a ref the wheel turns over that element."},
        "amount": {"type": "integer", "minimum": 1, "maximum": 20, "description": "scroll: wheel ticks (default 3)."},
        "region": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
                   "description": "screenshot: [x0, y0, x1, y1] in CSS pixels of the visible viewport, to zoom on a part."},
        "value": {"type": "string", "minLength": 1, "description": "select: the option value or its visible text."},
        "ignore_cache": {"type": "boolean", "description": "reload: bypass the browser cache."},
        "seconds": {"type": "number", "minimum": 0.2, "maximum": 10,
                    "description": "wait: seconds to give the page (a check page, a late list) before reading it again."},
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
                "text": {"type": "boolean", "default": False,
                         "description": "Return the page as reading lines (text in document order, controls as "
                                        "[role ref] label) instead of AX rows. Best for reading lists, prices and buttons."},
                "visible": {"type": "boolean", "default": False,
                            "description": "Only what is on screen now (after a scroll): rows/lines outside the viewport "
                                           "are left out and the result carries viewport {x, y, width, height, "
                                           "page_width, page_height} to know where on the page you are."},
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
            ("scroll", ["tab_id"]), ("hover", ["tab_id", "ref"]), ("select", ["tab_id", "ref", "value"]),
            ("reload", ["tab_id"]), ("back", ["tab_id"]), ("forward", ["tab_id"]),
            ("open_tab", ["url"]), ("close_tab", ["tab_id"]), ("wait", ["tab_id"]),
            ("session_save", ["tab_id", "name"]), ("session_open", ["name"]), ("session_forget", ["name"]),
            ("adopt_tab", ["tab_id"]), ("handoff", ["tab_id"]), ("handoff_status", ["tab_id"]),
            ("har_start", ["tab_id"]), ("har_stop", ["tab_id", "output_path"]),
            ("recording_start", ["tab_id", "output_path"]), ("recording_stop", ["tab_id"]),
            ("last_minute", ["tab_id", "output_path"]),
        ]
    ] + [{"if": {"required": ["view"]},
          "then": {"properties": {"action": {"enum": list(READBACK_ACTIONS)}}}},
        {"if": {"required": ["region"]}, "then": {"properties": {"action": {"const": "screenshot"}}}},
        {"if": {"required": ["direction"]}, "then": {"properties": {"action": {"const": "scroll"}}}},
        {"if": {"required": ["files"]}, "then": {"properties": {"action": {"const": "upload"}}}},
        {"if": {"required": ["output_path"]}, "then": {"properties": {"action": {"enum": ["download", "har_stop", "recording_start", "recording_stop", "last_minute"]}}}},
        {"if": {"required": ["cursor"]}, "then": {"properties": {"action": {"const": "recording_start"}}}},
        {"if": {"required": ["include_bodies"]}, "then": {"properties": {"action": {"const": "har_start"}}}},
        {"if": {"required": ["name"]}, "then": {"properties": {"action": {"enum": ["session_save", "session_open", "session_forget", "adopt_tab", "handoff"]}}}},
        {"if": {"required": ["reason"]}, "then": {"properties": {"action": {"enum": ["adopt_tab", "handoff"]}}}},
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
    result = {k: value[k] for k in ("ok", "phase", "result" if value["ok"] else "error")}
    if not value["ok"] and isinstance(value.get("failure_video"), dict):
        result["failure_video"] = value["failure_video"]
    return result
