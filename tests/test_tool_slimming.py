"""Fitting tool schemas into a small window (FAUSTUS).

The invariant that matters more than the byte count: *no tool ever disappears*.
Dropping tools to save context makes the model fail at the one action it needed,
with no trace of why. So these check the capability surface is preserved, that
the shared schema singletons are never mutated, and that roomy windows are left
completely alone.
"""

import copy

from src.tool_slimming import ROOMY_CONTEXT, slim_tool_schemas


def schema(name, desc_len=900, params=True):
    s = {"type": "function", "function": {
        "name": name,
        "description": "D" * desc_len,
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "P" * 400},
            "deep": {"type": "object", "properties": {
                "inner": {"type": "string", "description": "I" * 400}}},
        }, "required": ["path"]},
    }}
    if not params:
        s["function"].pop("parameters")
    return s


def names(schemas):
    return [s["function"]["name"] for s in schemas]


class TestWhenItActs:
    def test_unknown_window_is_left_alone(self):
        schemas = [schema(f"t{i}") for i in range(20)]
        out, report = slim_tool_schemas(schemas, context_length=0)
        assert out is schemas and report["slimmed"] is False

    def test_roomy_window_is_left_alone(self):
        schemas = [schema(f"t{i}") for i in range(40)]
        out, report = slim_tool_schemas(schemas, context_length=ROOMY_CONTEXT + 1)
        assert out is schemas and report["slimmed"] is False

    def test_schemas_that_already_fit_are_left_alone(self):
        out, report = slim_tool_schemas([schema("t", desc_len=20)], context_length=16384)
        assert out[0]["function"]["description"] == "D" * 20
        assert report["slimmed"] is False

    def test_disabled_by_setting(self):
        schemas = [schema(f"t{i}") for i in range(30)]
        out, report = slim_tool_schemas(schemas, context_length=8192, enabled=False)
        assert out is schemas and report["slimmed"] is False


class TestWhatItDoes:
    def test_it_shrinks_and_reports(self):
        schemas = [schema(f"t{i}") for i in range(30)]
        out, report = slim_tool_schemas(schemas, context_length=8192)
        assert report["slimmed"] is True
        assert report["after"] < report["before"]
        assert report["saved"] == report["before"] - report["after"]

    def test_every_tool_and_parameter_survives(self):
        schemas = [schema(f"t{i}") for i in range(30)]
        out, _ = slim_tool_schemas(schemas, context_length=8192)
        assert names(out) == names(schemas)
        props = out[0]["function"]["parameters"]["properties"]
        assert set(props) == {"path", "deep"}
        assert out[0]["function"]["parameters"]["required"] == ["path"]
        assert props["deep"]["properties"]["inner"]["type"] == "string"

    def test_nested_parameter_prose_is_clipped_too(self):
        schemas = [schema(f"t{i}") for i in range(30)]
        out, report = slim_tool_schemas(schemas, context_length=8192)
        inner = out[0]["function"]["parameters"]["properties"]["deep"]["properties"]["inner"]
        assert len(inner["description"]) < 400
        assert inner["description"].endswith("…")

    def test_the_shared_singletons_are_never_mutated(self):
        """FUNCTION_TOOL_SCHEMAS is module-level and shared by every request."""
        schemas = [schema(f"t{i}") for i in range(30)]
        pristine = copy.deepcopy(schemas)
        slim_tool_schemas(schemas, context_length=4096)
        assert schemas == pristine

    def test_tighter_window_gets_a_tighter_limit(self):
        schemas = [schema(f"t{i}") for i in range(30)]
        _, loose = slim_tool_schemas(schemas, context_length=16384)
        _, tight = slim_tool_schemas(schemas, context_length=4096)
        assert tight["limit"] <= loose["limit"]
        assert tight["after"] <= loose["after"]

    def test_impossible_budget_still_returns_usable_schemas(self):
        """30 tools cannot fit 200 tokens; better slimmed-and-honest than empty."""
        schemas = [schema(f"t{i}") for i in range(30)]
        out, report = slim_tool_schemas(schemas, context_length=1024)
        assert names(out) == names(schemas)
        assert report["fits"] is False

    def test_schema_without_parameters_survives(self):
        schemas = [schema(f"t{i}", params=False) for i in range(30)]
        out, _ = slim_tool_schemas(schemas, context_length=8192)
        assert "parameters" not in out[0]["function"]

    def test_garbage_entries_do_not_raise(self):
        schemas = [schema(f"t{i}") for i in range(29)] + [None]
        out, report = slim_tool_schemas(schemas, context_length=8192)
        assert report["slimmed"] is True and len(out) == 30
