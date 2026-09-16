"""A15 - acceptance-parity case.

Contract (docs/spec/paridad/, A15): "Task needs detail omitted by
compaction" -> "Original evidence reacquired; reacquisition cost recorded".

Exercises the REAL midturn spill path (`context_compactor.spill_large_tool_
results`, which calls the real `src.context_overflow.persist` and writes a
real content-addressed file under a real, isolated `context_overflow` root)
and then the REAL reacquisition path this lot adds
(`src.context_overflow.read_overflow`) - nothing here mocks the module under
test.
"""
from __future__ import annotations

import hashlib

import pytest

from src import context_compactor, context_overflow as overflow
from src.context_engine import store as ce_store
from tests.acceptance.conftest import record_evidence


@pytest.fixture()
def overflow_root(tmp_path, monkeypatch):
    root = tmp_path / "context_overflow"
    monkeypatch.setattr(overflow, "OVERFLOW_DIR", str(root))
    ce_store.use_path(str(tmp_path / "ce.db"))
    yield root
    ce_store.use_path(None)


@pytest.mark.acceptance("A15")
def test_spilled_body_is_reacquired_intact_and_the_run_report_records_its_cost(
    overflow_root, request,
):
    session_id = "s-a15"
    run_id = "run-a15"

    original_body = "STDOUT line follows with real data\n" + ("payload chunk " * 400)
    original_sha = hashlib.sha256(original_body.encode("utf-8", "replace")).hexdigest()

    messages = [
        {"role": "user", "content": "Run the full test suite and report failures."},
        {"role": "assistant", "content": "Running now.",
         "tool_calls": [{"id": "call_1", "type": "function",
                          "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": original_body},
        {"role": "user", "content": "What exactly failed in that run?"},
    ]

    spilled, spill_report = context_compactor.spill_large_tool_results(
        messages, session_id=session_id, keep_tool_rounds=0, spill_chars=100,
        durable=True, run_id=run_id, round_num=1,
    )
    assert spill_report["changed"] is True
    assert spill_report["spilled"] == 1
    overflow_id = spill_report["overflow_ids"][0]
    assert overflow_id == original_sha

    # The live prompt no longer carries the original body - only a stub.
    tool_msg = next(m for m in spilled if m.get("role") == "tool")
    assert original_body not in tool_msg["content"]
    assert f"[overflow id={overflow_id}" in tool_msg["content"]

    # The model decides it needs the omitted detail and re-acquires it by id.
    before_summary = overflow.reacquisition_summary(session_id, run_id)
    assert before_summary["reacquired_count"] == 0

    reacquired = overflow.read_overflow(
        session_id=session_id, content_sha256=overflow_id, run_id=run_id,
    )
    assert reacquired is not None
    reacquired_sha = hashlib.sha256(
        reacquired["content"].encode("utf-8", "replace")
    ).hexdigest()
    assert reacquired_sha == original_sha
    assert reacquired["content"] == original_body

    after_summary = overflow.reacquisition_summary(session_id, run_id)
    assert after_summary["reacquired_count"] == 1
    assert after_summary["reacquired_chars"] > 0
    assert after_summary["reacquired_chars"] == len(original_body)

    # The consultable per-session/run log the report reads back from.
    log = overflow.reacquisitions_for(session_id, run_id)
    assert len(log) == 1
    entry = log[0]
    assert entry["overflow_id"] == overflow_id
    assert entry["chars"] == len(original_body)
    assert entry["tokens_est"] > 0
    assert entry["at"]

    record_evidence(
        request,
        session_id=session_id,
        run_id=run_id,
        overflow_id=overflow_id,
        reacquired_chars=after_summary["reacquired_chars"],
    )
