"""Actual assistant prompt construction and native scheduled-chat delivery."""
import asyncio
import json
from datetime import datetime

import pytest

from tests.test_personal_assistants import database, client
from src import personal_assistants as assistants, assistant_runtime as runtime


def profile(**kwargs):
    return assistants.save("chat-a", "Research helper", "Notes stay descriptive", "luis", **kwargs)


def test_patch_preserves_configuration_and_native_task_identity(database, monkeypatch):
    from core.database import ScheduledTask, ChatMessage
    p = profile(instructions="Check primary sources", skills=["research"],
                proactivity={"enabled": True, "prompt": "Check updates", "timezone": "Europe/Madrid"})
    original_task = p["proactivity"]["task_id"]
    with client(monkeypatch) as c:
        renamed = c.patch("/api/mobile/assistants/chat-a", json={"name": "New name"}).json()
        assert renamed["instructions"] == "Check primary sources"
        assert renamed["skills"] == ["research"]
        assert renamed["description"] == "Notes stay descriptive"
        assert renamed["proactivity"]["task_id"] == original_task
        assert renamed["proactivity"]["enabled"]
        paused = c.patch("/api/mobile/assistants/chat-a", json={"proactivity": {"enabled": False}}).json()
        assert paused["proactivity"]["prompt"] == "Check updates"
        assert not paused["proactivity"]["enabled"]
    with database() as db:
        task = db.query(ScheduledTask).one()
        assert task.session_id == "chat-a" and task.name == "New name"
        assert task.status == "paused" and task.next_run is None
        assert task.email_results is False
        assert db.query(ChatMessage).count() == 1


def test_remove_restore_keeps_settings_but_does_not_restart_proactivity(database, monkeypatch):
    profile(instructions="Persistent role", skills=["test"], proactivity={"enabled": True, "prompt": "Check"})
    assistants.retire("chat-a", "luis")
    assert runtime.context_messages("chat-a", "luis") == []
    with client(monkeypatch) as c:
        restored = c.post("/api/mobile/assistants", json={"name": "Restored", "session_id": "chat-a"}).json()
    assert restored["instructions"] == "Persistent role"
    assert restored["skills"] == ["test"]
    assert not restored["proactivity"]["enabled"]
    assert restored["proactivity"]["next_run"] is None


@pytest.mark.parametrize("config", [
    {"enabled": True}, {"schedule": "unknown"}, {"timezone": "Mars/Base"},
    {"schedule": "cron", "cron": "bad"}, {"time": "25:90"}, {"day": 7},
    {"enabled": "yes"}, {"extra": "ignored?"},
])
def test_invalid_config_does_not_mutate_existing_profile_or_task(database, monkeypatch, config):
    before = profile(instructions="Keep this")
    with client(monkeypatch) as c:
        assert c.patch("/api/mobile/assistants/chat-a", json={"instructions": "Lost", "proactivity": config}).status_code == 422
    after = assistants.list_assistants("luis")[0]
    assert after["instructions"] == "Keep this"
    assert after["revision"] == before["revision"]
    assert after["proactivity"]["task_id"] is None


def test_pinned_skill_bodies_and_instructions_reach_real_prompt_outside_shared_cache(database, monkeypatch):
    from src import agent_loop as al
    from routes import prefs_routes
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: {})
    monkeypatch.setattr(runtime, "skill_catalog", lambda owner: [{"name": "source-check", "description": "Check citations", "procedure": ["VERIFY-PRIMARY-SOURCE"]}])
    profile(instructions="ROLE-ONLY-A", skills=["source-check"])
    def build(sid, **kwargs):
        built, _ = al._build_system_prompt([{"role": "user", "content": "Continue"}], "test", None, None, set(),
            owner="luis", session_id=sid, suppress_local_context=True, **kwargs)
        return built
    # Skills respect the request's suppression, instructions remain effective.
    first = build("chat-a")
    assert "ROLE-ONLY-A" in str(first)
    assert "VERIFY-PRIMARY-SOURCE" not in str(first)
    assert "ROLE-ONLY-A" not in str(build("not-an-assistant"))
    profile(instructions="ROLE-EDITED", skills=["source-check"])
    assert "ROLE-EDITED" in str(build("chat-a"))
    assert "ROLE-ONLY-A" not in str(build("chat-a"))
    messages = runtime.context_messages("chat-a", "luis")
    assert "VERIFY-PRIMARY-SOURCE" in str(messages)
    assert messages[1]["metadata"]["skill_disclosure"]
    assert runtime.context_messages("chat-a", "other") == []
    # Rebuild strips context rather than retaining an old assistant's role.
    stripped = al._strip_agent_injected_messages(first)
    assert "ROLE-ONLY-A" not in str(stripped)

    # Lean chat skips automatic retrieval, but keeps the owner's pinned skills.
    pinned, _ = al._build_system_prompt([{"role": "user", "content": "Continue"}], "test", None, None, set(),
        owner="luis", session_id="chat-a", suppress_skills=True, suppress_assistant_skills=False)
    assert "VERIFY-PRIMARY-SOURCE" in str(pinned)


def test_missing_skill_is_explicit_and_disabled_skills_leave_role(database, monkeypatch):
    profile(instructions="Role", skills=["gone"])
    monkeypatch.setattr(runtime, "skill_catalog", lambda owner: [])
    assert "Unavailable skills" in str(runtime.context_messages("chat-a", "luis"))
    assert "gone" not in str(runtime.context_messages("chat-a", "luis", skills_enabled=False))
    assert "Role" in str(runtime.context_messages("chat-a", "luis", skills_enabled=False))


def test_auth_disabled_chat_uses_the_same_local_owner_as_mobile_profiles(database, monkeypatch):
    from core.database import Session
    from src.owner_identity import DEFAULT_LOCAL_OWNER
    with database() as db:
        db.query(Session).filter_by(id="chat-a").update({"owner": DEFAULT_LOCAL_OWNER})
        db.commit()
    assistants.save("chat-a", "Local", "", DEFAULT_LOCAL_OWNER, instructions="LOCAL-ROLE")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert "LOCAL-ROLE" in str(runtime.context_messages("chat-a", None))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    assert runtime.context_messages("chat-a", None) == []


def test_hourly_uses_native_cron_and_persists_across_store_instances(database):
    from core.database import ScheduledTask
    p = profile(proactivity={"enabled": True, "prompt": "Check", "schedule": "hourly", "time": "09:23"})
    with database() as db:
        task = db.query(ScheduledTask).one()
        assert task.schedule == "cron" and task.cron_expression == "23 * * * *"
        assert task.next_run is not None and task.next_run.minute == 23
    observed = runtime.proactive_state("chat-a", "luis")
    assert observed["schedule"] == "hourly" and observed["enabled"]
    assert observed["next_run"] == p["proactivity"]["next_run"]


def test_native_scheduler_uses_same_chat_history_global_model_and_quiet_noop(database, monkeypatch):
    from core.database import ScheduledTask, ChatMessage, TaskRun
    from src.task_scheduler import TaskScheduler
    from src import agent_loop, tool_index, interactive_gate, task_endpoint
    from src.builtin_actions import TaskNoop
    from services import hoard_copilot
    monkeypatch.setattr(hoard_copilot, "global_model", lambda owner: {"endpoint_url": "http://global.invalid/v1/chat/completions", "model": "global-test", "headers": {"X-Test": "global"}})
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)
    async def quiet(*a, **k):
        return None
    monkeypatch.setattr(interactive_gate, "wait_for_interactive_quiet", quiet)
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", lambda **k: [])
    observed = []
    outputs = ["Update: new evidence", runtime.NO_UPDATE + runtime.NO_UPDATE]
    async def stream(**kwargs):
        observed.append(kwargs)
        yield "data: " + json.dumps({"delta": "Discarded draft"})
        yield "data: " + json.dumps({"type": "response_replace", "text": ""})
        yield "data: " + json.dumps({"delta": outputs.pop(0)})
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    profile(instructions="Research role", proactivity={"enabled": True, "prompt": "Check new sources", "quiet": True})
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._session_manager = None
    scheduler._still_owner_or_fenced = lambda tid: True
    with database() as db:
        task = db.query(ScheduledTask).one()
        result = asyncio.run(scheduler._execute_llm_task(task, db))
        assert result == "Update: new evidence"
        assert observed[0]["session_id"] == "chat-a"
        assert observed[0]["model"] == "global-test"
        assert observed[0]["headers"] == {"X-Test": "global"}
        assert observed[0]["fallbacks"] == []
        assert "Original result" in str(observed[0]["messages"])
        assert runtime.NO_UPDATE in observed[0]["messages"][-1]["content"]
        asyncio.run(scheduler._deliver_task_result(task, result, db))
        delivered = db.query(ChatMessage).filter_by(session_id="chat-a").all()
        assert len(delivered) == 3 and delivered[-1].content == result
        assert json.loads(delivered[-1].meta_data)["source"] == "assistant_proactivity"
        with pytest.raises(TaskNoop, match="No meaningful update"):
            asyncio.run(scheduler._execute_llm_task(task, db))
        assert db.query(ChatMessage).count() == 3  # no empty update or extra chat
        assert db.query(TaskRun).count() == 0  # outer native scheduler owns runs


def test_proactive_failure_is_not_replayed_without_tools(database, monkeypatch):
    from core.database import ScheduledTask
    from src.task_scheduler import TaskScheduler
    from src import tool_index
    from services import hoard_copilot
    monkeypatch.setattr(hoard_copilot, "global_model", lambda owner: {"endpoint_url": "http://global.invalid", "model": "global"})
    monkeypatch.setattr(tool_index, "get_tool_index", lambda: None)
    profile(proactivity={"enabled": True, "prompt": "Run"})
    scheduler = TaskScheduler.__new__(TaskScheduler)
    scheduler._still_owner_or_fenced = lambda tid: True
    async def failed(*a, **k):
        raise RuntimeError("tool run interrupted")
    scheduler._run_agent_loop = failed
    with database() as db:
        with pytest.raises(RuntimeError, match="tool run interrupted"):
            asyncio.run(scheduler._execute_llm_task(db.query(ScheduledTask).one(), db))


def test_run_now_checks_owner_and_does_not_enable_paused_mission(database, monkeypatch):
    from src import event_bus
    started = []
    class Scheduler:
        async def run_task_now(self, tid):
            started.append(tid)
            return True
    monkeypatch.setattr(event_bus, "_task_scheduler", Scheduler())
    profile(proactivity={"enabled": False, "prompt": "Check"})
    with client(monkeypatch) as c:
        assert c.post("/api/mobile/assistants/chat-b/run").status_code == 404
        assert c.post("/api/mobile/assistants/chat-a/run").status_code == 409
        assert started == []
        assert c.patch("/api/mobile/assistants/chat-a", json={"proactivity": {"enabled": True}}).status_code == 200
        assert c.post("/api/mobile/assistants/chat-a/run").json()["started"]
        assert started == [runtime.task_id("chat-a", "luis")]
