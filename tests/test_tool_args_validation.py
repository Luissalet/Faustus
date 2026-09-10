"""Unit tests for validate_tool_arguments / repair_tool_arguments (CALL-02,
CALL-03, QA-07).

QA-07: a wrong type, an extra/unknown field, and a scope-escaping path in
the same call must produce three distinct, localized errors, and bounded
repair must never make the path error disappear.
"""
import src.agent_tools  # noqa: F401  (breaks the tool_schemas<->agent_tools import cycle)
from src.tool_schemas import (
    PATH_ARGUMENT_FIELDS,
    ArgumentError,
    repair_tool_arguments,
    validate_tool_arguments,
)


# ---------------------------------------------------------------------------
# QA-07: three localized errors, repair does not erase the path error
# ---------------------------------------------------------------------------

def test_type_unknown_field_and_out_of_scope_path_are_three_localized_errors():
    args = {
        "source": "photo.png",
        "path": "../../etc/output.png",
        "format": "png",
        "quality": "90",   # wrong type: schema wants integer
        "bogus": 1,        # unknown field
    }
    errors = validate_tool_arguments("plan_media_transform", args)
    by_kind = {e.kind: e for e in errors}
    assert len(errors) == 3, [str(e) for e in errors]
    assert set(by_kind) == {"wrong_type", "unknown_field", "path_scope"}
    assert by_kind["wrong_type"].field == "quality"
    assert by_kind["unknown_field"].field == "bogus"
    assert by_kind["path_scope"].field == "path"


def test_repair_fixes_the_numeric_string_but_never_the_path_error():
    args = {
        "source": "photo.png",
        "path": "../../etc/output.png",
        "format": "png",
        "quality": "90",
        "bogus": 1,
    }
    errors = validate_tool_arguments("plan_media_transform", args)
    repaired, applied = repair_tool_arguments("plan_media_transform", args, errors)

    # The only repair applied is the exact-numeric-string coercion.
    assert len(applied) == 1
    assert applied[0]["field"] == "quality"
    assert applied[0]["to"] == 90

    # The path argument itself is byte-for-byte unchanged...
    assert repaired["path"] == "../../etc/output.png"
    # ...and re-validating the repaired args still reports the path error
    # (and the untouched unknown field) — repair narrowed the error set by
    # exactly the one fixable problem, nothing more.
    remaining = validate_tool_arguments("plan_media_transform", repaired)
    remaining_kinds = {e.kind for e in remaining}
    assert "path_scope" in remaining_kinds, "repair must never erase a path/scope error"
    assert "unknown_field" in remaining_kinds
    assert "wrong_type" not in remaining_kinds


def test_repair_does_not_mutate_the_original_args_dict():
    args = {"source": "a.png", "path": "out.png", "format": "png", "quality": "90"}
    errors = validate_tool_arguments("plan_media_transform", args)
    repair_tool_arguments("plan_media_transform", args, errors)
    assert args["quality"] == "90"  # original untouched; repaired is a copy


# ---------------------------------------------------------------------------
# CALL-02: individual checks
# ---------------------------------------------------------------------------

def test_enum_out_of_range_is_reported():
    args = {"source": "a.png", "path": "b.png", "format": "bmp"}
    errors = validate_tool_arguments("plan_media_transform", args)
    assert any(e.kind == "enum" and e.field == "format" for e in errors)


def test_missing_required_field_is_reported():
    args = {"source": "a.png", "format": "png"}  # "path" is required and missing
    errors = validate_tool_arguments("plan_media_transform", args)
    assert any(e.kind == "missing_required" and e.field == "path" for e in errors)


def test_valid_arguments_produce_no_errors():
    args = {"source": "a.png", "path": "out/b.png", "format": "png", "quality": 90}
    assert validate_tool_arguments("plan_media_transform", args) == []


def test_absolute_path_is_out_of_scope_for_a_marked_field():
    args = {"pattern": "TODO", "path": "/etc/passwd"}
    errors = validate_tool_arguments("grep", args)
    assert any(e.kind == "path_scope" and e.field == "path" for e in errors)


def test_relative_path_within_scope_is_not_flagged():
    args = {"pattern": "TODO", "path": "src/nested/dir"}
    errors = validate_tool_arguments("grep", args)
    assert not any(e.kind == "path_scope" for e in errors)


def test_absolute_path_is_not_flagged_for_tools_that_allow_real_paths():
    # read_file explicitly allows "any real path" (home folder, project
    # files, ...) — it is deliberately absent from PATH_ARGUMENT_FIELDS, so
    # an absolute path there is not a scope violation at this static layer.
    assert "path" not in PATH_ARGUMENT_FIELDS.get("read_file", set())
    args = {"path": "/home/user/notes.txt"}
    errors = validate_tool_arguments("read_file", args)
    assert not any(e.kind == "path_scope" for e in errors)


def test_unknown_tool_name_returns_no_errors():
    # Nothing to validate against; an unknown tool name is a different
    # failure caught earlier in the call pipeline, not this module's job.
    assert validate_tool_arguments("not_a_real_tool", {"anything": 1}) == []


def test_non_object_arguments_is_a_single_wrong_type_error():
    errors = validate_tool_arguments("plan_media_transform", ["not", "an", "object"])
    assert len(errors) == 1
    assert errors[0].kind == "wrong_type"
    assert errors[0].field == "$"


def test_repair_never_touches_a_field_with_no_fixable_error():
    # bash's "command" is a required string; there is nothing here that
    # repair could legitimately coerce, and it must not invent a fix.
    args = {"command": "echo hi"}
    errors = validate_tool_arguments("bash", args)
    repaired, applied = repair_tool_arguments("bash", args, errors)
    assert errors == []
    assert applied == []
    assert repaired == args


def test_argument_error_str_is_field_prefixed():
    err = ArgumentError("quality", "wrong_type", "expected integer, saw str ('90')", "90")
    assert str(err) == "quality: expected integer, saw str ('90')"
