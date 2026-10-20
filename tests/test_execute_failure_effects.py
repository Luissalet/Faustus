"""Execution failure is not proof that the command left no effects."""
import copy

import pytest

from src.tool_execution import format_tool_result
from src.tool_result import effect_certainty, normalize_tool_result


@pytest.mark.parametrize("tool", ["bash", "python", "powershell"])
@pytest.mark.parametrize("raw", [
    {"output": 'READY: {"url":"http://127.0.0.1:57598/"}\n201\n'
               'STDERR: TypeError: req() got multiple values for argument body', "exit_code": 1},
    {"error": "SyntaxError: invalid syntax", "exit_code": 1},
])
def test_dispatched_execution_failure_has_unknown_effects(tool, raw):
    before = copy.deepcopy(raw)
    result = normalize_tool_result(raw, call_id=tool)
    assert result.status == "failed"
    assert result.output == raw
    assert effect_certainty(result, effect_class="execute", raw=raw) == "unknown"
    assert raw == before


@pytest.mark.parametrize("raw,expected_status", [
    ({"error": "blocked", "blocked": True, "exit_code": 1}, "denied"),
    ({"error": "denied", "status": "denied", "exit_code": 1}, "denied"),
    ({"error": "no dispatch", "effect_not_dispatched": True, "exit_code": 1}, "failed"),
])
def test_no_dispatch_evidence_still_means_no_effect(raw, expected_status):
    result = normalize_tool_result(raw)
    assert result.status == expected_status
    assert effect_certainty(result, effect_class="execute", raw=raw) == "none"


@pytest.mark.parametrize("effect_class", ["read", "control"])
def test_failed_read_and_control_keep_their_existing_certainty(effect_class):
    raw = {"error": "validation failed", "exit_code": 1}
    assert effect_certainty(normalize_tool_result(raw), effect_class=effect_class, raw=raw) == "none"


def test_successful_execution_is_confirmed():
    raw = {"output": "completed", "exit_code": 0}
    result = normalize_tool_result(raw)
    assert result.status == "succeeded"
    assert effect_certainty(result, effect_class="execute", raw=raw) == "confirmed"


@pytest.mark.parametrize("certainty", ["unknown", "partial"])
@pytest.mark.parametrize("status", ["failed", None])
@pytest.mark.parametrize("shape", ["output", "stdout"])
def test_uncertain_effects_are_visible_alongside_original_evidence(certainty, status, shape):
    raw = {shape: "READY then 201", "exit_code": 1, "error": "original failure",
           "effect_certainty": certainty, "effect_id": "eff_example"}
    if shape == "stdout":
        raw["stderr"] = "original stderr"
    if status is not None:
        raw["status"] = status
    before = copy.deepcopy(raw)
    text = format_tool_result("execution failed", raw)
    assert f"Effect outcome: {certainty.upper()}" in text
    assert "Do not repeat it without checking the destination first" in text
    assert "eff_example" in text
    assert "READY then 201" in text and "original failure" in text
    assert "exit_code:** 1" in text
    if shape == "stdout":
        assert "original stderr" in text
    assert raw == before


def test_error_only_is_not_duplicated_and_no_effect_has_no_warning():
    raw = {"error": "original failure", "exit_code": 1, "effect_certainty": "none"}
    text = format_tool_result("failed", raw)
    assert text.count("original failure") == 1
    assert "Effect outcome:" not in text


@pytest.mark.parametrize("shape", ["output", "stdout"])
def test_error_is_not_hidden_by_a_shell_output_branch(shape):
    raw = {shape: "original output", "error": "original failure", "exit_code": 1}
    if shape == "stdout":
        raw["stderr"] = "original stderr"
    text = format_tool_result("failed", raw)
    assert text.count("original failure") == 1
    assert "original output" in text


@pytest.mark.parametrize("error", [False, None, ""])
def test_absent_error_is_not_rendered_as_a_new_error(error):
    text = format_tool_result("done", {"output": "original output", "error": error, "exit_code": 0})
    assert "**Error:**" not in text
