"""A31 — run-wide budget reservations for delegated work.

Trigger: parallel children (and a reviewer) consume the shared budget of one
run. Expect: reservations prevent uncontrolled overspend (a child whose
reservation would not fit under the run's ceiling is refused BEFORE it is
launched), and actual usage is reconciled against real token counts once a
child finishes — not against what was merely reserved.

Real code exercised: `src.agent_tools.subagent_tools.DelegateAgentsTool`
(the real `delegate_agents` tool, driving real `_run_subagent`/`one()`),
`src.budget_account` (the real SQLite-backed ledger — no mock of the module
under test), and `routes.budget_routes.setup_budget_routes` mounted on a
real FastAPI `TestClient`. Only the LLM (`stream_agent_loop`) is faked.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import budget_account as ba
from src.agent_tools import subagent_tools as st

from tests.acceptance.conftest import record_evidence


def _ev(obj):
    return "data: " + json.dumps(obj) + "\n\n"


def _harness_summary(mutations, stop_reason="complete"):
    return _ev({"type": "harness_summary", "data": {"mutations": mutations, "stop_reason": stop_reason}})


class _SM:
    def __init__(self):
        self.sessions = {}

    def create_session(self, session_id, **kw):
        self.sessions[session_id] = type(
            "S", (), {"messages": [], "add_message": lambda self, m: self.messages.append(m)})()

    def get_session(self, sid):
        return self.sessions.get(sid)

    def save_sessions(self):
        pass


@pytest.mark.acceptance("A31")
@pytest.mark.asyncio
async def test_parallel_children_reserve_and_reconcile_against_shared_run_budget(tmp_path, monkeypatch, request):
    # ── isolate the ledger from every other test/process ───────────────────
    ledger_path = tmp_path / "budget_accounts.sqlite3"
    monkeypatch.setattr(ba, "default_path", lambda: ledger_path)

    # ── wire delegate_agents to a fake model route (real tool code) ────────
    import src.agent_loop as al
    import src.ai_interaction as ai
    from src import tool_execution as te

    monkeypatch.setattr(te, "get_active_workspace", lambda: str(tmp_path))
    monkeypatch.setattr(te, "get_active_workspace_roots", lambda: ())
    parent = type("P", (), {"endpoint_url": "http://127.0.0.1:11434/v1", "model": "m",
                            "headers": None, "name": "parent"})()
    sm = _SM()
    sm.sessions["parent"] = parent
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)

    run_id = "parent"  # agent_runs has no tracked run for this fake session → falls back to it

    knobs = {
        "agent_subagent_tick_seconds": 0.05, "agent_subagent_stall_seconds": 100,
        "agent_subagent_supervisor": True, "agent_subagent_max_parallel": 2,
        # A31: a tight run ceiling. `max_rounds: 2` below is clamped to the
        # parser's floor of 3 rounds, so each task reserves
        # rounds(3) * DEFAULT_RESERVE_TOKENS_PER_ROUND(6000) = 18000 tokens;
        # two of them at once (36000) do not fit under 20000.
        "agent_budget_tokens_per_run": 20000,
    }
    monkeypatch.setattr(st, "_setting", lambda k, d=None: knobs.get(k, d))

    # Each worker "really" uses only 1500 tokens (1000 in + 500 out) — far
    # less than its 12000-token reservation — so reconciliation has a real
    # surplus to release. asyncio.sleep forces a genuine suspension point so
    # BOTH children's reservations are attempted while both are in flight,
    # which is the A31 trigger ("parallel children ... consume remaining
    # shared budget").
    async def _loop(endpoint_url, model, messages, **kwargs):
        yield _ev({"type": "round_info", "round": 1, "input_tokens": 1000, "output_tokens": 500})
        await asyncio.sleep(0.05)
        yield _harness_summary([])
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_agent_loop", _loop)

    events = []

    async def _cb(payload):
        events.append(payload["subagent"])

    tool = st.DelegateAgentsTool()
    result = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [
            {"name": "a", "instruction": "one"},
            {"name": "b", "instruction": "two"},
        ],
        "parallel": True, "timeout_s": 60, "max_rounds": 2,
    }), {"session_id": "parent", "owner": None, "progress_cb": _cb}), 10)

    reps = {r["name"]: r for r in result["subagents"]}
    # ── overspend prevention: one child fits under the ceiling, the other
    # is refused BEFORE it ever runs (never got a session/child chat at all) ──
    succeeded = [n for n, r in reps.items() if not r.get("error")]
    refused = [n for n, r in reps.items() if r.get("error")]
    assert len(succeeded) == 1 and len(refused) == 1, reps
    refused_report = reps[refused[0]]
    assert refused_report["stop_reason"] == "budget_exceeded"
    assert "budget" in refused_report["error"].lower() or "budget_exceeded" in json.dumps(refused_report)
    refused_events = [e for e in events if e["name"] == refused[0] and e["event"] == "error"]
    assert refused_events and refused_events[0].get("budget", {}).get("error") == "budget_exceeded"
    # The refused child never got a child session — it was rejected before
    # a worker chat, a model call or a GPU slot was ever touched.
    assert reps[refused[0]].get("session_id") in (None, "")

    # ── reconciliation: real usage (1500), not the 12000-token reservation ──
    snap = ba.snapshot(run_id)
    assert snap["consumed_tokens"] == 1500, snap
    assert snap["reserved_tokens"] == 0, snap  # the winner's reservation closed on reconcile
    # unknown, never a false 0.0 — the fake loop never reported a price
    assert snap["consumed_cost"] == "unknown"
    assert snap["remaining_tokens"] == 20000 - 1500

    # ── the surplus IS actually usable: a later child now fits where it
    # would not have fit against the ORIGINAL 12000-token reservation ──────
    events2 = []

    async def _cb2(payload):
        events2.append(payload["subagent"])
    result2 = await asyncio.wait_for(tool.execute(json.dumps({
        "tasks": [{"name": "c", "instruction": "three"}],
        "timeout_s": 60, "max_rounds": 2,
    }), {"session_id": "parent", "owner": None, "progress_cb": _cb2}), 10)
    assert not result2["subagents"][0].get("error"), result2["subagents"][0]

    # ── GET /api/runs/{run_id}/budget serves the same ledger over a real
    # route, mounted on a real FastAPI TestClient (not called as a function) ─
    from routes.budget_routes import setup_budget_routes
    app = FastAPI()
    app.include_router(setup_budget_routes())
    client = TestClient(app)
    # This install runs with LOCALHOST_BYPASS (no login) per the project's
    # own setup — matches `require_user`'s AUTH_ENABLED=false branch rather
    # than standing up the full cookie/auth_manager stack for one GET.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    resp = client.get(f"/api/runs/{run_id}/budget")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["consumed_tokens"] >= 1500 + 1500  # a + c, both reconciled by now
    assert body["consumed_cost"] == "unknown"

    record_evidence(
        request,
        module="src.budget_account", tool="src.agent_tools.subagent_tools.DelegateAgentsTool",
        route="GET /api/runs/{run_id}/budget (routes/budget_routes.py, not yet wired into app.py — see T7_wiring.md)",
        run_id=run_id, refused_child=refused[0], succeeded_child=succeeded[0],
        snapshot_after_first_delegation=snap,
    )
