"""Boolean replacement mode is checked before path, read, write or history hooks."""
import asyncio
import json

import pytest

from src import doubt_review, tool_execution
from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import filesystem_tools as tools
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block


@pytest.fixture
def edit_target(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.txt"
    path.write_text("foo foo", encoding="utf-8")
    calls = {"resolve": 0, "read": 0, "write": 0, "history": 0}
    monkeypatch.setattr(doubt_review, "enabled", lambda: False)

    def resolve(raw):
        calls["resolve"] += 1
        assert raw == str(path)
        return raw

    monkeypatch.setattr(tool_execution, "_resolve_tool_path", resolve)
    for name, counter in [("_read_text_lf", "read"), ("_write_text_lf", "write")]:
        original = getattr(tools, name)

        def counted(*args, _original=original, _counter=counter, **kwargs):
            calls[_counter] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(tools, name, counted)

    def history(*args, **kwargs):
        calls["history"] += 1

    monkeypatch.setattr(tools, "record_edit_history", history)
    return path, calls


def invoke(path, extra, native):
    args = {"path": str(path), "old_string": "foo", "new_string": "bar", **extra}
    content = json.dumps(args)
    if native:
        block = function_call_to_tool_block("edit_file", content)
        assert block.tool_type == "edit_file"
        assert json.loads(block.content) == args
        content = block.content
    return asyncio.run(TOOL_HANDLERS["edit_file"](content, {}))


@pytest.mark.parametrize("native", [False, True], ids=["fence", "native"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], [False], {}, {"replace_all": False}])
def test_invalid_mode_leaves_file_and_all_hooks_untouched(edit_target, value, native):
    path, calls = edit_target
    result = invoke(path, {"replace_all": value}, native)
    assert result == {"error": "edit_file: replace_all must be a boolean", "exit_code": 1}
    assert path.read_text(encoding="utf-8") == "foo foo"
    assert calls == {"resolve": 0, "read": 0, "write": 0, "history": 0}


@pytest.mark.parametrize("native", [False, True], ids=["fence", "native"])
@pytest.mark.parametrize("extra", [{}, {"replace_all": False}])
def test_false_or_default_preserves_ambiguous_match_rejection(edit_target, extra, native):
    path, calls = edit_target
    result = invoke(path, extra, native)
    assert result["exit_code"] == 1 and "not unique" in result["error"]
    assert path.read_text(encoding="utf-8") == "foo foo"
    assert calls == {"resolve": 1, "read": 1, "write": 0, "history": 0}


@pytest.mark.parametrize("native", [False, True], ids=["fence", "native"])
@pytest.mark.parametrize("extra,initial,expected", [
    ({"replace_all": True}, "foo foo", "bar bar"),
    ({}, "foo tail", "bar tail"),
    ({"replace_all": False}, "foo tail", "bar tail"),
])
def test_valid_mode_writes_and_records_exactly_one_edit(edit_target, extra, initial, expected, native):
    path, calls = edit_target
    path.write_text(initial, encoding="utf-8")
    result = invoke(path, extra, native)
    assert result["exit_code"] == 0
    assert path.read_text(encoding="utf-8") == expected
    assert calls == {"resolve": 1, "read": 1, "write": 1, "history": 1}


def test_original_coercion_would_replace_both_matches(edit_target):
    path, _ = edit_target
    status, updated = tools._replace_text(path.read_text(encoding="utf-8"), "foo", "bar", bool("false"))
    assert (status, updated) == ("ok", "bar bar")


def test_native_schema_declares_optional_boolean():
    definition = next(entry["function"] for entry in FUNCTION_TOOL_SCHEMAS
                      if entry["function"]["name"] == "edit_file")
    parameters = definition["parameters"]
    assert parameters["properties"]["replace_all"]["type"] == "boolean"
    assert "replace_all" not in parameters["required"]
