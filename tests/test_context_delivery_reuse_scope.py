from dataclasses import replace

import pytest

from src.context_engine import wiring
from src.context_engine.contracts import (
    ContextActor, ContextBudget, ContextExecution, ContextItem, ContextPacket,
    ContextPolicy, ContextRequest, ContextSection, ContextTask,
)


@pytest.fixture
def delivery(monkeypatch):
    calls = []
    monkeypatch.setattr(wiring, "enabled", lambda: True)
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **kw: 6000)
    monkeypatch.setattr(wiring, "_remember_omitted", lambda *a: [])

    async def compile_live(request, **kw):
        # This synthetic compiler deliberately does not query objectives.
        from src.context_engine.objective_reuse import record_query
        record_query(None, (), False)
        calls.append(request)
        return ContextPacket(
            packet_id=f"ctxpkt_{len(calls)}", request_id=request.request_id,
            owner=request.execution.owner, session_id=request.execution.session_id,
            model=request.actor.model,
            window=ContextBudget(max_tokens=4096, input_budget=900),
            sections=(ContextSection(kind="retrieved_memory", items=(ContextItem(
                item_id="m", source_type="memory", source_ref="mem:private",
                title="Snapshot", body=f"snapshot {len(calls)}", tokens=30),)),),
        ), []

    monkeypatch.setattr(wiring, "_compile_live", compile_live)
    request = ContextRequest(request_id="ctxreq_one", created_at="2026-09-29T00:00:00Z",
        actor=ContextActor(agent_id="agent", role="agent", model="model"),
        execution=ContextExecution(owner="alice", project_id="p1", session_id="s1", turn_id="t1"),
        task=ContextTask(query="Review draft."))
    return request, calls


@pytest.mark.parametrize("part,changes", [
    ("execution", {"owner": "bob"}), ("execution", {"project_id": "p2"}),
    ("execution", {"session_id": "s2"}), ("execution", {"workspace": "/new"}),
    ("execution", {"turn_id": "t2"}), ("execution", {"run_id": "run2"}),
    ("execution", {"branch_id": "branch2"}), ("execution", {"council_id": "c2"}),
    ("actor", {"model": "new-model"}), ("actor", {"participant_id": "worker2"}),
    ("actor", {"role": "worker"}),
    ("policy", {"allow_personal_memory": False}),
    ("policy", {"allow_project_sources": False}),
    ("policy", {"excluded_refs": ("mem:private",)}),
    ("policy", {"excluded_prefixes": ("private/",)}),
    ("policy", {"minimum_freshness_s": 10}),
    ("task", {"query": "Another task."}), ("task", {"phase": "plan"}),
])
@pytest.mark.asyncio
async def test_changed_scope_policy_or_task_recompiles(delivery, part, changes):
    request, calls = delivery
    first = await wiring.deliver_round(request=request, messages=[])
    updated = replace(request, **{part: replace(getattr(request, part), **changes)})
    second = await wiring.deliver_round(request=updated, messages=[], previous=first)
    assert len(calls) == 2
    assert second["message"] is not first["message"]
    assert not second["report"].get("reused")


@pytest.mark.asyncio
async def test_pinned_refs_change_recompiles_and_legacy_has_no_reuse_identity(delivery):
    request, calls = delivery
    first = await wiring.deliver_round(request=request, messages=[])
    await wiring.deliver_round(request=replace(request, explicit_refs=("doc:new",)), messages=[], previous=first)
    legacy = {k: v for k, v in first.items() if k != "_reuse_scope"}
    await wiring.deliver_round(request=request, messages=[], previous=legacy)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_attempt_id_time_deadline_and_fitting_budget_keep_same_snapshot(delivery, monkeypatch):
    request, calls = delivery
    first = await wiring.deliver_round(request=request, messages=[])
    monkeypatch.setattr(wiring, "timeout_s", lambda: 0.01)
    updated = replace(request, request_id="ctxreq_two", created_at="2026-09-30T00:00:00Z",
                      policy=replace(request.policy, token_budget=4000))
    second = await wiring.deliver_round(request=updated, messages=[], previous=first, round_index=2)
    third = await wiring.deliver_round(request=updated, messages=[], previous=second, round_index=3)
    assert len(calls) == 1
    assert first["message"] is second["message"] is third["message"]
    assert third["report"]["reused"] is True
    assert len(first["_reuse_scope"]) == 64
    assert "_reuse_scope" not in first["report"]
    assert "_reuse_scope" not in first["message"]["metadata"]
