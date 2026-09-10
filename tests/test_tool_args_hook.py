"""CALL-02/CALL-03 argument validation, wired in at the exact point a
native function call becomes a ToolBlock (src/agent_loop.py's
`_validate_native_tool_call` / `_resolve_tool_blocks`).

Reverting `_resolve_tool_blocks` to call `function_call_to_tool_block` alone
(dropping the `_validate_native_tool_call` step) makes every test here fail:
`arg_validation` would come back empty and a call with bad arguments would
reach the executor unblocked.
"""
import json

import pytest

import src.agent_tools  # noqa: F401 - resolves circular schema imports first
import src.agent_loop as agent_loop
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block, validate_tool_arguments


def _native_call(name: str, args: dict) -> dict:
    return {"name": name, "arguments": json.dumps(args)}


# ---------------------------------------------------------------------- #
# _validate_native_tool_call — direct, focused checks
# ---------------------------------------------------------------------- #

def test_unrepairable_enum_error_blocks_in_strict_mode(monkeypatch):
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)  # default: strict
    args = {"query": "faustus release notes", "time_filter": "century"}
    block = function_call_to_tool_block("web_search", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("web_search", json.dumps(args), block)

    assert meta is not None
    assert meta["blocked"] is True
    assert any(e["field"] == "time_filter" and e["kind"] == "enum" for e in meta["errors"])
    assert meta["repairs"] == []  # enum is not a fixable_kind
    assert new_block is block  # nothing to rebuild — no repair changed anything

    result = agent_loop._tool_arg_error_result(meta["errors"])
    assert result["blocked"] is True
    assert "time_filter" in result["error"]
    assert "schema.type_mismatch" in result["error"]  # enum -> schema.type_mismatch


def test_repairable_numeric_string_is_fixed_and_executes_with_repaired_value(monkeypatch):
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)
    args = {"path": "app.py", "limit": "50"}  # limit is declared "integer"
    block = function_call_to_tool_block("read_file", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("read_file", json.dumps(args), block)

    assert meta is not None
    assert meta["blocked"] is False
    assert meta["repairs"] == [
        {"field": "limit", "from": "50", "to": 50, "reason": "numeric string coerced to the schema's declared number type"}
    ]
    # The block actually used for execution carries the REPAIRED value, not
    # the original string — read_file's content is JSON once offset/limit
    # is present (src/tool_schemas.py's function_call_to_tool_block).
    assert new_block is not block
    payload = json.loads(new_block.content)
    assert payload["limit"] == 50
    assert isinstance(payload["limit"], int)


def test_warn_mode_never_blocks_but_still_reports(monkeypatch):
    monkeypatch.setenv("FAUSTUS_TOOL_ARG_VALIDATION", "warn")
    args = {}  # missing required "pattern"
    block = function_call_to_tool_block("grep", json.dumps(args))
    assert block is not None  # grep isn't in _REQUIRED_NATIVE_TOOL_ARGS, so this converts fine

    new_block, meta = agent_loop._validate_native_tool_call("grep", json.dumps(args), block)

    assert meta is not None
    assert meta["blocked"] is False
    assert any(e["field"] == "pattern" and e["kind"] == "missing_required" for e in meta["errors"])
    assert new_block is block


def test_off_mode_skips_validation_entirely(monkeypatch):
    monkeypatch.setenv("FAUSTUS_TOOL_ARG_VALIDATION", "off")
    args = {"query": "x", "time_filter": "century"}  # would be a blocking enum error otherwise
    block = function_call_to_tool_block("web_search", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("web_search", json.dumps(args), block)

    assert meta is None
    assert new_block is block


def test_unknown_field_never_blocks_but_is_reported(monkeypatch):
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)  # strict
    args = {"pattern": "TODO", "not_a_real_field": True}
    block = function_call_to_tool_block("grep", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("grep", json.dumps(args), block)

    assert meta is not None
    assert meta["blocked"] is False  # unknown_field is never blocking, even in strict mode
    assert any(e["field"] == "not_a_real_field" and e["kind"] == "unknown_field" for e in meta["errors"])
    assert new_block is block


def test_missing_required_field_is_reported_but_the_tool_still_answers(monkeypatch):
    """A missing argument is the tool's own error to give (it always was),
    and the offer/execute coherence suite drives every offered tool with
    `{}`: refusing here would turn "offered" into "blocked". So strict mode
    reports it and lets the call through."""
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)
    args = {}
    block = function_call_to_tool_block("grep", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("grep", json.dumps(args), block)

    assert meta is not None
    assert meta["blocked"] is False
    assert any(e["field"] == "pattern" and e["kind"] == "missing_required" for e in meta["errors"])
    assert new_block is block


def test_path_scope_error_is_never_repaired_and_blocks(monkeypatch):
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)
    args = {"path": "../../etc/passwd"}
    block = function_call_to_tool_block("ls", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("ls", json.dumps(args), block)

    assert meta is not None
    assert meta["blocked"] is True
    assert any(e["field"] == "path" and e["kind"] == "path_scope" for e in meta["errors"])
    assert meta["repairs"] == []

    result = agent_loop._tool_arg_error_result(meta["errors"])
    assert "schema.path_scope" in result["error"]


def test_clean_call_reports_nothing(monkeypatch):
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)
    args = {"path": "app.py"}
    block = function_call_to_tool_block("read_file", json.dumps(args))
    assert block is not None

    new_block, meta = agent_loop._validate_native_tool_call("read_file", json.dumps(args), block)

    assert meta is None
    assert new_block is block


# ---------------------------------------------------------------------- #
# update_plan's own schema (task 2): `steps` alone must validate cleanly,
# with no `plan` required. Reverting FUNCTION_TOOL_SCHEMAS' update_plan
# entry's `"required": []` back to `"required": ["plan"]` makes this fail
# with a missing_required error on `plan`.
# ---------------------------------------------------------------------- #

def test_update_plan_schema_does_not_require_plan_when_steps_given():
    schema = next(
        s["function"]["parameters"] for s in FUNCTION_TOOL_SCHEMAS
        if s["function"]["name"] == "update_plan"
    )
    assert "plan" not in (schema.get("required") or [])

    errors = validate_tool_arguments("update_plan", {"steps": [{"title": "Do the thing"}]})
    assert not any(e.field == "plan" for e in errors)


# ---------------------------------------------------------------------- #
# _resolve_tool_blocks — the actual call site, keyed by id(block)
# ---------------------------------------------------------------------- #

def test_resolve_tool_blocks_keys_arg_validation_by_block_id(monkeypatch):
    monkeypatch.delenv("FAUSTUS_TOOL_ARG_VALIDATION", raising=False)
    native_calls = [
        _native_call("web_search", {"query": "x", "time_filter": "century"}),  # blocked
        _native_call("read_file", {"path": "app.py"}),  # clean
    ]
    # The return stays the three values every older caller unpacks; what
    # validation found travels through the dict the caller hands in.
    arg_validation = {}
    tool_blocks, used_native, converted_calls = agent_loop._resolve_tool_blocks(
        "", native_calls, round_num=1, arg_validation=arg_validation,
    )

    assert used_native is True
    assert len(tool_blocks) == 2
    assert len(converted_calls) == 2

    blocked_block = tool_blocks[0]
    clean_block = tool_blocks[1]
    assert arg_validation.get(id(blocked_block)) is not None
    assert arg_validation[id(blocked_block)]["blocked"] is True
    assert id(clean_block) not in arg_validation
