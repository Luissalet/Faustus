# -*- coding: utf-8 -*-
"""The approval card has to describe what actually happened.

Seen while using the app: asking Faustus to run the tests in a local folder
produced "External untrusted context has already influenced this run."
Nothing external had happened -- it had read two files the user pointed it
at. Reading a local file does arm this gate, and should, because a file can
carry an instruction; but a card that says "external" when nothing external
occurred is a card people learn to click past.
"""

from src.tool_capabilities import ToolRunSecurityContext, tool_result_should_arm_gate


def _armed_gate(*tools):
    gate = ToolRunSecurityContext()
    for name in tools:
        gate.observe_tool_result(name, {"success": True, "output": "some text"})
    return gate


def test_reading_a_local_file_still_arms_the_gate():
    """The safety behaviour is unchanged -- only the wording is."""
    assert tool_result_should_arm_gate("read_file", {"success": True, "output": "x"}) is True
    assert _armed_gate("read_file").external_untrusted_context_seen is True


def test_the_card_names_the_tools_that_brought_the_content_in():
    decision = _armed_gate("read_file", "bash").decision_for("bash", "pytest")
    assert decision.allowed is False
    assert "read_file" in decision.reason and "bash" in decision.reason


def test_the_card_does_not_call_a_local_file_external():
    reason = _armed_gate("read_file").decision_for("bash", "pytest").reason
    assert "External untrusted context" not in reason
    assert "did not write itself" in reason


def test_the_card_still_says_which_effect_needs_the_go_ahead():
    assert "execute_code" in _armed_gate("read_file").decision_for("bash", "pytest").reason
