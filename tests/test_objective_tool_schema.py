"""The model must retain the delta contract even when tool prose is trimmed."""
import ast
from pathlib import Path


def test_objective_delta_fields_are_structural_not_only_prose():
    import copy
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    schema = copy.deepcopy(next(s["function"] for s in FUNCTION_TOOL_SCHEMAS
                                if s["function"]["name"] == "project_objectives"))

    def remove_prose(value):
        if isinstance(value, dict):
            value.pop("description", None)
            for child in value.values():
                remove_prose(child)
        elif isinstance(value, list):
            for child in value:
                remove_prose(child)

    remove_prose(schema)
    delta = schema["parameters"]["properties"]["deltas"]["items"]
    fields = delta["properties"]
    assert fields["op"]["enum"] == ["ADD", "EDIT", "KILL"]
    assert fields["status"]["enum"] == ["open", "in_progress", "blocked", "done", "dropped"]
    assert fields["title"]["maxLength"] == 200
    assert fields["notes"]["type"] == "string"
    assert fields["deps"]["items"]["type"] == "string"
    assert "op" in delta["required"]
    assert delta["additionalProperties"] is False
