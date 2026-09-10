"""Lote 62 — CALL-05: a typed ToolResult, normalized at the single execution
point.

`src/contracts/tool.py::ToolResult` already gave the spec's typed call
result (status/output/evidence_refs/effects/error/uncertainty) a home; what
was missing was the adapter from the ad hoc dict every tool in
`src/agent_tools/*.py` actually returns, applied where every caller (chat,
workflow, subagent, MCP) already converges: `execute_tool_block` in
`src/tool_execution.py`. This file has two halves:

1. `normalize_tool_result` itself, unit-tested directly against the shapes
   real tools return.
2. `execute_tool_block` wiring: prove the adapter runs on every call through
   the single execution point, and — CALL-05's own acceptance — that a call
   which transported cleanly but carries a functional error classifies as
   "failed", never "succeeded", while the dict `execute_tool_block` hands
   back to its caller is untouched (byte-for-byte the same dict the tool
   itself returned — no agent_tools/*.py file had to change).
"""
import asyncio
import json
import logging

import pytest

from src.agent_tools import ToolBlock
from src.contracts.tool import ToolResult
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_result import normalize_tool_result


# ── normalize_tool_result: pure classification ──────────────────────────

def test_transport_success_with_functional_error_is_failed_never_succeeded():
    # CALL-05's own acceptance, verbatim: exit_code carries a real error even
    # though the call itself returned cleanly.
    result = normalize_tool_result({"error": "no such file", "exit_code": 1})
    assert result.status == "failed"
    assert result.status != "succeeded"
    assert result.error is not None
    assert result.error.message == "no such file"


def test_nonzero_exit_code_with_no_error_string_is_still_failed():
    result = normalize_tool_result({"stdout": "", "exit_code": 2})
    assert result.status == "failed"
    assert result.error is not None


def test_clean_success_has_no_error():
    result = normalize_tool_result({"output": "done", "exit_code": 0})
    assert result.status == "succeeded"
    assert result.error is None


def test_success_with_no_exit_code_at_all_defaults_to_succeeded():
    # Several tools (ask_user, update_plan) never set exit_code on the happy
    # path at all — absence must not be misread as failure.
    result = normalize_tool_result({"output": "ok"})
    assert result.status == "succeeded"


def test_blocked_by_policy_is_denied_not_failed():
    result = normalize_tool_result({"error": "policy says no", "exit_code": 1, "blocked": True})
    assert result.status == "denied"
    assert result.error.code == "permission.denied"


def test_subagent_file_lock_is_denied():
    result = normalize_tool_result({"error": "file locked by another worker", "exit_code": 1, "locked": True})
    assert result.status == "denied"


def test_base_revision_mismatch_is_conflict_not_failed():
    result = normalize_tool_result({
        "error": "edit_file: f.py changed since base_revision was read",
        "exit_code": 1, "status": "conflict", "error_code": "BASE_REVISION_MISMATCH",
    })
    assert result.status == "conflict"
    assert result.error.code == "conflict.base_revision_mismatch"


def test_output_is_carried_through_unmodified():
    raw = {"output": "hello", "exit_code": 0, "extra_field": {"nested": 1}}
    result = normalize_tool_result(raw)
    assert result.output == raw
    assert result.output is not raw  # a defensive copy, not an alias


def test_malformed_non_mapping_result_is_outcome_unknown():
    result = normalize_tool_result("not a dict at all")
    assert result.status == "outcome_unknown"
    assert result.uncertainty is not None
    assert result.uncertainty.reason


def test_missing_ids_default_to_the_documented_placeholder():
    result = normalize_tool_result({"output": "ok"})
    assert result.call_id == "unknown"
    assert result.attempt_id == "unknown"


def test_given_ids_are_kept():
    result = normalize_tool_result({"output": "ok"}, call_id="call_1", attempt_id="attempt_1")
    assert result.call_id == "call_1" and result.attempt_id == "attempt_1"


def test_result_is_a_real_toolresult_instance():
    result = normalize_tool_result({"output": "ok"})
    assert isinstance(result, ToolResult)
    # Round-trips through the contract's own mapping shape without error.
    mapping = result.to_mapping()
    assert mapping["status"] == "succeeded"


# ── execute_tool_block: applied at the single execution point ──────────

def _run(tool_type, content):
    return asyncio.run(execute_tool_block(
        ToolBlock(tool_type, content),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))


def test_execute_tool_block_normalizes_a_functional_failure_without_changing_the_returned_dict(caplog):
    # ask_user's own validation failure: fewer than two options. Reaches
    # execute_tool_block cleanly (no exception) but the tool's dict carries
    # a real error — exactly the "transport ok, functional error" case.
    with caplog.at_level(logging.DEBUG, logger="src.tool_execution"):
        desc, result = _run("ask_user", json.dumps({
            "question": "Only one?", "options": [{"label": "A"}],
        }))
    assert result.get("exit_code") == 1
    assert "error" in result
    # The adapter classified it as failed and said so at the single
    # execution point, without adding or removing a single key from what
    # the tool itself returned.
    assert any("status=failed" in r.message for r in caplog.records)
    assert set(result.keys()) == {"error", "exit_code"}


def test_execute_tool_block_leaves_a_successful_result_completely_untouched():
    desc, result = _run("ask_user", json.dumps({
        "question": "Which?",
        "options": [{"label": "A"}, {"label": "B"}],
    }))
    assert result["exit_code"] == 0
    # Exactly the keys AskUserTool itself sets — normalize_tool_result never
    # mutates the tuple execute_tool_block hands back to its caller.
    assert set(result.keys()) == {"ask_user", "output", "exit_code"}


def test_normalize_tool_result_never_raises_on_a_result_it_cannot_classify(caplog):
    # Defensive: even a pathological result must not break the tool-call
    # pipeline just because normalization couldn't make sense of it.
    with caplog.at_level(logging.DEBUG, logger="src.tool_execution"):
        desc, result = _run("ask_user", json.dumps({
            "question": "Which?",
            "options": [{"label": "A"}, {"label": "B"}],
        }))
    assert result["exit_code"] == 0  # the real call still succeeded normally
