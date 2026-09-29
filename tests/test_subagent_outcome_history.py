import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database, models, session_manager
from src import agent_loop, agent_runs, ai_interaction
from src.agent_harness import TurnLedger
from src.agent_tools import subagent_tools as st


@pytest.fixture
def worker_history(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "workers.db").as_posix())
    factory = sessionmaker(bind=engine)
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(session_manager, "SessionLocal", factory)
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", None)
    manager = session_manager.SessionManager()
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", manager)
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: None)
    monkeypatch.setattr(agent_runs, "mark_busy", lambda *a: None)
    monkeypatch.setattr(agent_runs, "clear_busy", lambda *a: None)

    async def execute(extra):
        events = []

        async def stream(*a, **kw):
            yield "data: " + json.dumps({"type": "tool_output", "tool": "web_fetch",
                "exit_code": 0, "output": "Page", "call_id": "call_source", **extra}) + "\n\n"

        async def emit(event):
            events.append(event)

        monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
        run = st.SubagentRun(0, {"name": "worker", "instruction": "Read a source."})
        await st._run_subagent(run, endpoint_url="http://unused", model="test", headers=None,
            owner=None, workspace=None, workspace_roots=None, max_rounds=1, shared_context="",
            parent_session_id=None, emit=emit, save_transcript=False)
        manager.create_session(session_id=run.session_id, name="Worker", endpoint_url="http://unused", model="test")
        st._save_transcript(run, manager)
        reopened = session_manager.SessionManager().get_session(run.session_id)
        message = reopened.history[-1].to_dict()
        ledger = TurnLedger()
        ledger.note_prior_message(message)
        return run, events, message, ledger

    yield execute
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["partial", "outcome_unknown", "cancelled", "failed", "denied", "conflict", "succeeded"])
@pytest.mark.parametrize("exit_code", [None, 0])
async def test_outcome_survives_sse_worker_sqlite_reload_and_harness(worker_history, status, exit_code):
    run, events, message, ledger = await worker_history({"result_status": status, "exit_code": exit_code,
        "uncertainty": {"reason": "r" * 700, "reconcile_action": "check", "private_blob": "excluded"}})
    output = next(e for e in events if e.get("phase") == "done")
    stored = message["metadata"]["tool_events"][0]
    assert output["result_status"] == stored["result_status"] == status
    assert output["ok"] is (status == "succeeded")
    assert run.failed_calls == (0 if status == "succeeded" else 1)
    assert stored["call_id"] == "call_source"
    assert stored["uncertainty"] == {"reason": "r" * 512, "reconcile_action": "check"}
    assert ledger.prior_pages is (status == "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,expected", [({}, "succeeded"), ({"status": "ok"}, "succeeded"),
    ({"exit_code": 1}, "failed"), ({"result_status": []}, "outcome_unknown"),
    ({"result_status": "new-state"}, "outcome_unknown"), ({"status": []}, "outcome_unknown")])
async def test_legacy_and_malformed_events_do_not_fabricate_historical_success(worker_history, extra, expected):
    _, events, message, ledger = await worker_history(extra)
    assert message["metadata"]["tool_events"][0]["result_status"] == expected
    assert ledger.prior_pages is (expected == "succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"error": "failed"}, {"blocked": True},
    {"approval_required": True}, {"exit_code": 1}, {"status": None}, {"status": "partial"}])
async def test_conflicting_success_cannot_turn_false_into_true_after_reload(worker_history, extra):
    run, events, message, ledger = await worker_history({"result_status": "succeeded", **extra})
    output = next(e for e in events if e.get("phase") == "done")
    assert output["ok"] is False
    assert run.failed_calls == 1
    assert message["metadata"]["tool_events"][0]["result_status"] == "outcome_unknown"
    assert not ledger.prior_pages
