"""update_plan accepts the todo shapes models actually send and never stores
raw JSON as the checklist (19 real calls failed on {"steps": [{"content", ...}]})."""
import asyncio
import json

from src.agent_tools.interaction_tools import UpdatePlanTool


def run(payload):
    return asyncio.run(UpdatePlanTool().execute(payload, None))[1]


def test_content_and_completed_steps_make_a_plan():
    res = run(json.dumps({"steps": [{"content": "look", "status": "completed"},
                                     {"content": "fix", "status": "in_progress"}]}))
    assert res["exit_code"] == 0
    assert [s["status"] for s in res["plan_update"]["steps"]] == ["done", "pending"]
    assert "look" in res["plan_update"]["plan"] and "fix" in res["plan_update"]["plan"]


def test_empty_steps_is_an_error_not_a_json_plan():
    for payload in ('{"steps": []}', '{"steps": [{"status": "done"}]}', '{"plan": ""}', '{}'):
        res = run(payload)
        assert res.get("exit_code") == 1, payload
        assert "plan_update" not in res


def test_plain_markdown_still_works():
    res = run("- [x] one\n- [ ] two")
    assert res["exit_code"] == 0
    assert [s["title"] for s in res["plan_update"]["steps"]] == ["one", "two"]