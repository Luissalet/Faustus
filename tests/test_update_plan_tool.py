"""`update_plan` — the agent writes back to the active plan (tick done / revise).

Pure UI-control marker: `execute_tool_block` returns a `plan_update` payload the
agent loop turns into a `plan_update` SSE event; the frontend replaces the stored
plan and refreshes the docked plan window. No I/O, does not end the turn.
"""
import asyncio
import json

from src.agent_tools import ToolBlock, TOOL_TAGS  # import first to avoid circular
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
from src.tool_security import is_public_blocked_tool


def _run(content):
    return asyncio.run(execute_tool_block(
        ToolBlock("update_plan", content),
        security_context=NO_TOOL_SECURITY_CONTEXT,
    ))


def test_valid_plan_returns_marker_and_counts():
    plan = "- [x] step one\n- [ ] step two\n- [ ] step three"
    desc, result = _run(json.dumps({"plan": plan}))
    assert result.get("exit_code") == 0
    assert result["plan_update"]["plan"] == plan
    assert "1/3" in result["output"]   # 1 done of 3


def test_plain_string_accepted():
    plan = "- [ ] a\n- [x] b"
    _, result = _run(plan)
    assert result["plan_update"]["plan"] == plan


def test_empty_rejected():
    _, result = _run(json.dumps({"plan": "   "}))
    assert "error" in result and result.get("exit_code") == 1


def test_registered_everywhere():
    assert "update_plan" in TOOL_TAGS
    assert "update_plan" in ALWAYS_AVAILABLE
    assert "update_plan" in BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "update_plan" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    # Not admin/public-gated — any user can drive their own plan.
    assert is_public_blocked_tool("update_plan") is False


# --- TASK-01: no truncation, structured steps alongside the markdown -------

def test_long_plan_is_not_truncated():
    # This is exactly the case the old `plan = plan[:8192]` line broke: a
    # plan whose markdown is longer than the cap used to lose its tail
    # silently, with no error and no trace.
    lines = [f"- [{'x' if i % 2 == 0 else ' '}] step {i:04d} with some extra descriptive text"
             for i in range(400)]
    plan = "\n".join(lines)
    assert len(plan) > 8192
    _, result = _run(json.dumps({"plan": plan}))
    assert result["plan_update"]["plan"] == plan  # untouched, full length
    assert len(result["plan_update"]["steps"]) == 400
    assert result["plan_update"]["steps"][-1]["title"].endswith("0399 with some extra descriptive text")


def test_plan_update_payload_carries_structured_steps_and_revision():
    plan = "- [x] done step\n- [ ] pending step"
    _, result = _run(json.dumps({"plan": plan}))
    payload = result["plan_update"]
    assert payload["plan"] == plan
    assert [s["status"] for s in payload["steps"]] == ["done", "pending"]
    assert isinstance(payload["revision"], int) and payload["revision"] >= 1
    assert payload["warnings"] == []


def test_model_marked_done_step_is_unverified_until_evidence():
    # A checkbox the model itself ticked is `status: done` but not trusted
    # as verified until real evidence arrives — the field must exist and
    # to_markdown must not drop it.
    _, result = _run(json.dumps({"plan": "- [x] fixed the bug"}))
    step = result["plan_update"]["steps"][0]
    assert step["status"] == "done"
    assert step["verified"] is False


def test_structured_json_steps_input_is_accepted():
    payload = {"steps": [
        {"title": "investigate", "status": "done"},
        {"title": "write the fix", "status": "pending"},
    ]}
    _, result = _run(json.dumps(payload))
    assert result.get("exit_code") == 0
    out = result["plan_update"]
    assert "- [x] investigate" in out["plan"]
    assert "- [ ] write the fix" in out["plan"]
    assert [s["title"] for s in out["steps"]] == ["investigate", "write the fix"]


def test_unparseable_line_produces_a_warning_but_keeps_the_plan():
    plan = "- free-form note, no checkbox\n- [ ] real step"
    _, result = _run(json.dumps({"plan": plan}))
    payload = result["plan_update"]
    assert payload["plan"] == plan  # markdown field still verbatim
    assert len(payload["steps"]) == 1
    assert payload["warnings"]
