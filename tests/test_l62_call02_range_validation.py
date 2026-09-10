"""Lote 62 — CALL-02: range validation in validate_tool_arguments.

`src/tool_schemas.py::validate_tool_arguments` already caught wrong types,
unknown fields, out-of-enum values and out-of-scope paths — but several
schemas declare `minimum`/`maximum`/`minLength`/`maxLength`/`minItems`/
`maxItems` (e.g. `plan_media_transform.quality` is 1-100, `read_file.line_count`
is 1-500) that were never actually checked here: a call with `quality: 9999`
or `limit: -5` passed this validator clean. This file is the regression test
for the fix: an out-of-range value is now reported as its own localized
`range` error, never silently accepted or clamped.
"""
import src.agent_tools  # noqa: F401  (breaks the tool_schemas<->agent_tools import cycle)
from src.tool_schemas import repair_tool_arguments, validate_tool_arguments


def test_numeric_value_above_maximum_is_a_range_error():
    args = {"source": "a.png", "path": "b.png", "format": "png", "quality": 9999}
    errors = validate_tool_arguments("plan_media_transform", args)
    assert len(errors) == 1
    assert errors[0].kind == "range"
    assert errors[0].field == "quality"


def test_numeric_value_below_minimum_is_a_range_error():
    args = {"source": "a.png", "path": "b.png", "format": "png", "quality": 0}
    errors = validate_tool_arguments("plan_media_transform", args)
    assert len(errors) == 1
    assert errors[0].kind == "range" and errors[0].field == "quality"


def test_value_within_bounds_is_not_a_range_error():
    args = {"source": "a.png", "path": "b.png", "format": "png", "quality": 90}
    assert validate_tool_arguments("plan_media_transform", args) == []


def test_boundary_values_are_accepted():
    for boundary in (1, 100):
        args = {"source": "a.png", "path": "b.png", "format": "png", "quality": boundary}
        assert validate_tool_arguments("plan_media_transform", args) == []


def test_line_count_above_max_is_reported():
    # project_context's line_count: minimum 1, maximum 500.
    args = {"action": "read", "path": "f.py", "line_count": 5000}
    errors = validate_tool_arguments("project_context", args)
    assert any(e.kind == "range" and e.field == "line_count" for e in errors)


def test_start_line_below_minimum_is_reported():
    args = {"action": "read", "path": "f.py", "start_line": 0}
    errors = validate_tool_arguments("project_context", args)
    assert any(e.kind == "range" and e.field == "start_line" for e in errors)


def test_bool_is_never_checked_as_a_numeric_range():
    # A schema whose field allows both boolean and integer must never treat
    # `True`/`False` as the magnitudes 1/0 for a minimum/maximum bound —
    # same exclusion _type_matches already applies.
    from src.tool_schemas import _range_violation
    assert _range_violation("x", True, {"type": "integer", "minimum": 5}) is None


def test_range_error_is_never_offered_to_bounded_repair():
    # CALL-03's repair must never clamp a value into range — that changes
    # what the caller asked for. A range error stays a range error after
    # repair_tool_arguments runs.
    args = {"source": "a.png", "path": "b.png", "format": "png", "quality": 9999}
    errors = validate_tool_arguments("plan_media_transform", args)
    repaired, applied = repair_tool_arguments("plan_media_transform", args, errors)
    assert applied == []
    assert repaired["quality"] == 9999
    remaining = validate_tool_arguments("plan_media_transform", repaired)
    assert any(e.kind == "range" for e in remaining)


def test_string_length_bounds_are_enforced():
    # update_plan's todo "title": minLength 1, maxLength 200.
    args = {"steps": [{"title": "x" * 500, "priority": 1}]}
    # Reach the nested schema indirectly via a top-level string field that
    # declares minLength/maxLength — session_tools' pinned_version sibling
    # `title` field lives nested, so exercise _range_violation directly for
    # the string-length branch (top-level FUNCTION_TOOL_SCHEMAS fields with
    # minLength/maxLength are the same shape one level up).
    from src.tool_schemas import _range_violation
    err = _range_violation("title", "x" * 500, {"type": "string", "minLength": 1, "maxLength": 200})
    assert err is not None and err.kind == "range"
    ok = _range_violation("title", "short title", {"type": "string", "minLength": 1, "maxLength": 200})
    assert ok is None


def test_array_item_count_bounds_are_enforced():
    from src.tool_schemas import _range_violation
    err = _range_violation("options", [1, 2, 3], {"type": "array", "maxItems": 2})
    assert err is not None and err.kind == "range"
    ok = _range_violation("options", [1, 2], {"type": "array", "maxItems": 2})
    assert ok is None
