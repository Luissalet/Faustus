"""Frozen worker provenance through real SQLite, without model requests."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database, models, session_manager
from src import agent_runs, ai_interaction, budget_account
from src.agent_tools import subagent_tools as st


@pytest.fixture
def history(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + (tmp_path / "causal.db").as_posix())
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(session_manager, "SessionLocal", factory)
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", None)
    manager = session_manager.SessionManager()
    monkeypatch.setattr(models, "_SESSION_MANAGER_INSTANCE", manager)
    monkeypatch.setattr(ai_interaction, "get_session_manager", lambda: manager)
    monkeypatch.setattr(budget_account, "default_path", lambda: tmp_path / "budget.db")
    manager.create_session(session_id="parent", name="Parent", endpoint_url="http://unused", model="fixture")
    yield manager
    engine.dispose()


def reload_metadata(child):
    reopened = session_manager.SessionManager().get_session(child)
    return [m.metadata["subagent"] for m in reopened.history
            if m.role == "assistant" and "subagent" in (m.metadata or {})]


@pytest.mark.asyncio
async def test_real_dispatch_freezes_parent_for_workers_and_reviewer(history, monkeypatch):
    current = ["run-original"]
    monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id=current[0], status="running"))
    seen = []

    async def worker(run, **kwargs):
        seen.append(run)
        assert run.parent_run_id == "run-original"
        current[0] = "run-replacement"
        monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id=current[0], status="running"))
        run.parent_session_id = kwargs["parent_session_id"]
        run.session_id = run.id
        history.create_session(session_id=run.session_id, name=run.name, endpoint_url="http://unused", model="fixture")
        run.text, run.stop_reason = "Synthetic result", "complete"
        run.mutations = ["synthetic.txt"]

    monkeypatch.setattr(st, "_run_subagent", worker)
    result = await st.DelegateAgentsTool().execute(json.dumps({
        "tasks": [{"name": "one", "instruction": "Inspect a fixture"},
                  {"name": "two", "instruction": "Inspect another fixture"}],
        "parallel": False, "reviewer": True}), {"session_id": "parent"})
    assert "error" not in result
    assert len(seen) == 3 and seen[-1].role == "reviewer"
    assert len({r.delegation_id for r in seen}) == 1
    for run in seen:
        meta = reload_metadata(run.session_id)[-1]
        assert meta["worker_id"] == run.id
        assert meta["session_id"] == run.session_id
        assert meta["parent_run_id"] == "run-original"
        assert meta["delegation_id"] == seen[0].delegation_id
        assert "parent_call_id" not in meta


@pytest.mark.asyncio
async def test_concurrent_delegations_have_independent_causal_identity(history, monkeypatch):
    monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id="real-run", status="running"))
    seen = []
    ready = asyncio.Event()

    async def worker(run, **kwargs):
        seen.append(run)
        if len(seen) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), timeout=3)
        run.parent_session_id = kwargs["parent_session_id"]
        run.session_id = run.id
        history.create_session(session_id=run.id, name=run.name, endpoint_url="http://unused", model="fixture")
        run.text, run.stop_reason = "Synthetic result", "complete"

    monkeypatch.setattr(st, "_run_subagent", worker)
    content = json.dumps({"tasks": [{"name": "same", "instruction": "Inspect fixture"}], "reviewer": False})
    await asyncio.gather(*(st.DelegateAgentsTool().execute(content, {"session_id": "parent"}) for _ in range(2)))
    metadata = [reload_metadata(r.id)[-1] for r in seen]
    assert len({m["delegation_id"] for m in metadata}) == 2
    assert len({m["worker_id"] for m in metadata}) == 2
    assert {m["parent_run_id"] for m in metadata} == {"real-run"}


def test_resumed_child_preserves_distinct_invocations_and_legacy(history):
    history.create_session(session_id="child", name="Child", endpoint_url="http://unused", model="fixture")
    history.get_session("child").add_message(models.ChatMessage("assistant", "Legacy",
        metadata={"subagent": {"name": "same", "parent_session": "parent"}}))
    runs = []
    for parent in ("run-a", "run-b"):
        run = st.SubagentRun(0, {"name": "same", "instruction": "Fixture", "resume": {"kind": "session", "id": "child"}})
        run.parent_session_id, run.session_id = "parent", "child"
        run.text, run.stop_reason = "Result", "complete"
        st._bind_causal_identity(run, parent, "delegation-" + parent)
        st._bind_causal_identity(run, "replacement", "replacement")
        st._save_transcript(run, history)
        runs.append(run)
    metadata = reload_metadata("child")
    assert "worker_id" not in metadata[0]
    assert [m["parent_run_id"] for m in metadata[1:]] == ["run-a", "run-b"]
    assert [m["worker_id"] for m in metadata[1:]] == [r.id for r in runs]


@pytest.mark.parametrize("parent,result", [(None, "ignored"), ("parent", None), ("parent", "real")])
def test_parent_run_never_falls_back_to_session(history, monkeypatch, parent, result):
    monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id=result, status="running"))
    run = st.SubagentRun(0, {"name": "same", "instruction": "Fixture"})
    run.session_id = "parent"
    st._bind_causal_identity(run, st._causal_parent_run_id(parent), "delegation")
    st._save_transcript(run, history)
    assert reload_metadata("parent")[-1]["parent_run_id"] == (result if parent else None)


@pytest.mark.parametrize("status", ["done", "error", "cancelled", "interrupted"])
def test_previous_terminal_run_is_not_causal_parent(history, monkeypatch, status):
    monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id="old-run", status=status))
    assert agent_runs.get_run_id("parent") == "old-run"
    assert st._causal_parent_run_id("parent") is None


@pytest.mark.asyncio
async def test_real_retry_keeps_invocation_identity(history, monkeypatch):
    monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id="run-before", status="running"))
    observed = []

    async def worker(run, **kwargs):
        observed.append((run.id, run.parent_run_id, run.delegation_id))
        monkeypatch.setitem(agent_runs._RUNS, "parent", SimpleNamespace(run_id="run-after", status="running"))
        run.parent_session_id = kwargs["parent_session_id"]
        if not run.session_id:
            run.session_id = run.id
            history.create_session(session_id=run.id, name=run.name, endpoint_url="http://unused", model="fixture")
        run.text, run.stop_reason = "Done.", "complete"
        if len(observed) > 1:
            run.mutations = ["report.md"]

    monkeypatch.setattr(st, "_run_subagent", worker)
    await st.DelegateAgentsTool().execute(json.dumps({"tasks": [
        {"name": "writer", "instruction": "Write a file report.md with the report"}],
        "reviewer": False}), {"session_id": "parent"})
    assert len(observed) == 2 and observed[0] == observed[1]
    assert reload_metadata(observed[0][0])[-1]["parent_run_id"] == "run-before"
