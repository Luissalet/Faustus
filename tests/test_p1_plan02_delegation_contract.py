"""PLAN-02 — delegation with contract and budget.

Acceptance: a child returns `partial` when it does not fulfil its assignment;
success is never declared just because it produced text.
"""
from __future__ import annotations

from src.agent_tools import subagent_tools as sa


# ── verify_criteria: the deterministic, evidence-only check ────────────────

def test_no_tool_calls_means_every_criterion_is_unmet():
    """Text alone is not evidence (PLAN-02's own acceptance wording)."""
    result = sa.verify_criteria(
        ["the login bug is fixed"], mutations=[], tool_calls=0, error=None,
    )
    assert result["complete"] is False
    assert result["unmet"] == ["the login bug is fixed"]
    assert result["criteria"][0]["met"] is False


def test_criterion_naming_a_file_needs_that_file_mutated():
    unmet = sa.verify_criteria(
        ["update auth.py to fix the token check"], mutations=["src/other.py"],
        tool_calls=3, error=None,
    )
    assert unmet["complete"] is False
    assert "auth.py" in unmet["unmet"][0]

    met = sa.verify_criteria(
        ["update auth.py to fix the token check"], mutations=["src/auth.py"],
        tool_calls=3, error=None,
    )
    assert met["complete"] is True


def test_an_errored_run_meets_no_criterion():
    result = sa.verify_criteria(["do the thing"], mutations=["x.py"], tool_calls=5,
                                error="boom: exit 1")
    assert result["complete"] is False


def test_criteria_without_a_file_token_pass_on_any_real_tool_evidence():
    result = sa.verify_criteria(["summarize the repository"], mutations=[],
                                tool_calls=2, error=None)
    assert result["complete"] is True


# ── parse_delegation_args: the contract accepts "criteria" ─────────────────

def test_parse_delegation_args_carries_criteria_through():
    import json
    payload = json.dumps({
        "tasks": [{
            "name": "fix-bug", "instruction": "fix the login bug",
            "criteria": ["auth.py is updated", "  ", "tests still pass"],
        }],
    })
    parsed = sa.parse_delegation_args(payload)
    assert parsed["tasks"][0]["criteria"] == ["auth.py is updated", "tests still pass"]


def test_parse_delegation_args_without_criteria_is_unchanged():
    """No `criteria` key at all: the row must be byte-for-byte what this
    parser produced before PLAN-02 (COMUN rule 3 — no capacity lost)."""
    import json
    payload = json.dumps({"tasks": [{"name": "t", "instruction": "do something"}]})
    parsed = sa.parse_delegation_args(payload)
    assert "criteria" not in parsed["tasks"][0]


# ── SubagentRun.report(): the parent verifies, the child does not decide ───

def _run_with(*, stop_reason: str, criteria=None, mutations=None, tool_calls=0,
              error=None) -> sa.SubagentRun:
    task = {"name": "worker-1", "instruction": "do the assigned work"}
    if criteria:
        task["criteria"] = criteria
    run = sa.SubagentRun(0, task)
    run.stop_reason = stop_reason
    run.mutations = list(mutations or [])
    run.tool_calls = tool_calls
    run.error = error
    return run


def test_child_claiming_complete_with_no_tool_evidence_reports_partial():
    run = _run_with(stop_reason="complete",
                     criteria=["the report is written to report.md"],
                     mutations=[], tool_calls=0)
    report = run.report()
    assert report["status"] == "partial"
    assert report["criteria_check"]["complete"] is False


def test_child_claiming_complete_with_matching_evidence_reports_done():
    run = _run_with(stop_reason="complete",
                     criteria=["update report.md with the findings"],
                     mutations=["report.md"], tool_calls=4)
    report = run.report()
    assert report["status"] == "done"
    assert report["criteria_check"]["complete"] is True


def test_child_report_without_criteria_is_unaffected():
    """No criteria stated at all: report() must behave exactly as it did
    before PLAN-02 — no `criteria_check` key, status driven by stop_reason
    alone."""
    run = _run_with(stop_reason="complete", mutations=[], tool_calls=0)
    report = run.report()
    assert report["status"] == "done"
    assert "criteria_check" not in report
