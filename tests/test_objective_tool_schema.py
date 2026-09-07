"""The model must retain the delta contract even when tool prose is trimmed."""
import ast
from pathlib import Path


def test_objective_delta_fields_are_structural_not_only_prose():
    source = Path(__file__).resolve().parent.parent / "src" / "tool_schemas.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "FUNCTION_TOOL_SCHEMAS"
                              for t in node.targets))
    schema = next(s["function"] for s in ast.literal_eval(assignment.value)
                  if s["function"]["name"] == "project_objectives")

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
