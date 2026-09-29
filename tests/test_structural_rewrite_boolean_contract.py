"""Rewrite mode agrees with the native schema before invoking either backend."""
import asyncio
import json

import pytest

from src.agent_tools import TOOL_HANDLERS
from src.agent_tools import structural_search_tools as tools
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block


@pytest.fixture
def backend_calls(monkeypatch):
    calls = []

    def backend(mode):
        def run(*args, **kwargs):
            calls.append(mode)
            return {"output": mode, "exit_code": 0}
        return run

    monkeypatch.setattr(tools.structural_search, "rewrite_preview", backend("preview"))
    monkeypatch.setattr(tools.structural_search, "rewrite_apply", backend("apply"))
    return calls


def invoke(extra, native):
    args = {"pattern": "foo($A)", "rewrite": "bar($A)", "lang": "python", "path": "synthetic.py"}
    args.update(extra)
    content = json.dumps(args)
    if native:
        block = function_call_to_tool_block("structural_rewrite", content)
        assert block.tool_type == "structural_rewrite"
        assert json.loads(block.content) == args
        content = block.content
    return asyncio.run(TOOL_HANDLERS["structural_rewrite"](content, {}))


@pytest.mark.parametrize("native", [False, True], ids=["fence", "native"])
@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], [False], {}, {"apply": False}])
def test_invalid_apply_never_invokes_backend(backend_calls, value, native):
    result = invoke({"apply": value}, native)
    assert result == {"error": "structural_rewrite: apply must be a boolean", "exit_code": 1}
    assert backend_calls == []


@pytest.mark.parametrize("native", [False, True], ids=["fence", "native"])
@pytest.mark.parametrize("extra,mode", [({}, "preview"), ({"apply": False}, "preview"), ({"apply": True}, "apply")])
def test_valid_apply_selects_exactly_one_backend(backend_calls, extra, mode, native):
    result = invoke(extra, native)
    assert result == {"output": mode, "exit_code": 0}
    assert backend_calls == [mode]


def test_native_schema_declares_optional_boolean():
    definition = next(entry["function"] for entry in FUNCTION_TOOL_SCHEMAS
                      if entry["function"]["name"] == "structural_rewrite")
    parameters = definition["parameters"]
    assert parameters["properties"]["apply"]["type"] == "boolean"
    assert "apply" not in parameters["required"]
