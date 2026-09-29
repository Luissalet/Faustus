import pytest

from src.tool_presentation import tool_result_fields


@pytest.mark.parametrize("status", ["partial", "outcome_unknown", "cancelled"])
@pytest.mark.parametrize("code", [None, 0, 1])
def test_status_and_uncertainty_survive_presentation(status, code):
    raw = {"status": status, "exit_code": code,
           "uncertainty": {"reason": "Connection lost", "reconcile_action": "inspect_state"}}
    fields = tool_result_fields(raw)
    assert fields["result_status"] == status
    assert fields["uncertainty"] == raw["uncertainty"]


@pytest.mark.parametrize("raw,status", [
    ({"exit_code": 0}, "succeeded"), ({"error": "failed", "exit_code": 1}, "failed"),
    ({"timed_out": True, "exit_code": 124}, "outcome_unknown"),
    ({"blocked": True}, "denied"), (None, "outcome_unknown"),
])
def test_presentation_uses_shared_normalizer(raw, status):
    assert tool_result_fields(raw)["result_status"] == status


def test_only_bounded_uncertainty_is_forwarded_without_raw_payload():
    result = tool_result_fields({"status": "partial", "secret": "private", "uncertainty": {
        "reason": "x" * 1000, "reconcile_action": {"secret": "private"}, "token": "private"}})
    assert result == {"result_status": "partial", "uncertainty": {"reason": "x" * 512}}
