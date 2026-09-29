import pytest

from src.agent_harness import TurnLedger


@pytest.mark.parametrize("status", ["partial", "outcome_unknown", "cancelled", "failed", "denied", "conflict"])
@pytest.mark.parametrize("code", [None, 0])
@pytest.mark.parametrize("field", ["status", "result_status"])
def test_unsuccessful_result_cannot_verify_progress_or_prior_source(status, code, field):
    result = {field: status}
    if code is not None:
        result["exit_code"] = code
    ledger = TurnLedger(user_text="Write a report.")
    event = ledger.record("write_file", "report.txt\nReport", result, 1)
    progress = ledger.record_progress([{"content": "Write report", "status": "completed"}], 1)
    assert event["ok"] is False
    assert progress[0]["verified"] is False
    assert progress[0]["mutation_backed"] is False
    assert ledger.mutations == []
    ledger.note_prior_message({"role": "assistant", "metadata": {
        "tool_events": [{"tool": "web_fetch", **result}]}})
    assert ledger.prior_pages is False
    assert ledger.prior_sources is False


@pytest.mark.parametrize("result", [
    {}, {"exit_code": 0}, {"status": "succeeded"}, {"result_status": "succeeded"},
    {"status": "ok"}, {"status": "ready"}, {"status": "completed"},
])
def test_explicit_success_and_legacy_results_keep_evidence(result):
    ledger = TurnLedger()
    assert ledger.record("write_file", "report.txt\nReport", result)["ok"] is True
    progress = ledger.record_progress([{"content": "Write report", "status": "completed"}], 1)
    assert progress[0]["verified"] is True
    ledger.note_prior_message({"role": "assistant", "metadata": {
        "tool_events": [{"tool": "web_fetch", **result}]}})
    assert ledger.prior_pages is True


@pytest.mark.parametrize("result", [
    {"result_status": "ready"}, {"result_status": []}, {"result_status": None},
    {"status": []}, {"status": {}}, {"status": None},
    {"result_status": "succeeded", "status": "partial"},
    {"status": "succeeded", "exit_code": 1},
    {"status": "succeeded", "approval_required": True},
    {"status": "succeeded", "error": "failed"},
])
def test_malformed_or_contradictory_result_does_not_verify(result):
    ledger = TurnLedger()
    assert ledger.record("write_file", "report.txt\nReport", result)["ok"] is False
    ledger.note_prior_message({"role": "assistant", "metadata": {
        "tool_events": [{"tool": "web_fetch", **result}]}})
    assert ledger.prior_pages is False


@pytest.mark.parametrize("status", ["partial", "outcome_unknown", "cancelled", "failed", "denied", "conflict"])
def test_announced_call_cannot_override_uncertain_stored_result(status):
    ledger = TurnLedger()
    ledger.note_prior_message({"role": "assistant", "tool_calls": [
        {"function": {"name": "web_fetch"}}], "metadata": {
        "tool_events": [{"tool": "web_fetch", "result_status": status, "exit_code": 0}]}})
    assert not ledger.prior_pages


def test_announced_call_alone_is_not_evidence_but_legacy_tool_output_is():
    ledger = TurnLedger()
    ledger.note_prior_message({"role": "assistant", "tool_calls": [{"function": {"name": "web_fetch"}}]})
    assert not ledger.prior_pages
    ledger.note_prior_message({"role": "tool", "name": "web_fetch", "content": "Page text"})
    assert ledger.prior_pages
    ledger.note_prior_message({"role": "tool", "name": "web_fetch", "result_status": "failed"})
    assert ledger.prior_pages  # later failure cannot erase a real prior read


@pytest.mark.parametrize("extra", [
    {"result_status": "outcome_unknown"},
    {"metadata": {"result_status": "partial"}},
    {"content": {"status": "failed"}},
    {"content": '{"status":"cancelled"}'},
    {"metadata": {"tool_events": [{"tool": "web_fetch", "result_status": "denied"}]}},
])
def test_legacy_tool_fallback_honors_explicit_failure(extra):
    ledger = TurnLedger()
    ledger.note_prior_message({"role": "tool", "name": "web_fetch", "content": "Page text", **extra})
    assert not ledger.prior_pages
