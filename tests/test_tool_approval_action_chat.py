"""A chat single-action answer executes its seal, then rearms the usual gate."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.tool_approvals import ToolApprovalStore
from src.tool_approval_scopes import ToolApprovalScope, CHAT_SESSION_APPROVAL_CONTEXT_MARKER
from src.tool_capabilities import capabilities_for_action
from test_foreground_model_routing import _chat_stream_endpoint


def pending(store, *, session_id="session-1", tool="bash", content="printf exact"):
    return store.create(owner="alice", session_id=session_id, origin_run_id="original",
                        tool_name=tool, content=content, workspace=None,
                        external_untrusted_context_seen=True, continuation_query="Inspect the project",
                        capabilities=capabilities_for_action(tool, content))


def test_http_chat_accepts_single_action_without_granting_task_or_chat(monkeypatch):
    import routes.chat_routes as route
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    store = ToolApprovalStore()
    monkeypatch.setattr(route, "tool_approval_store", store)
    card = pending(store)
    app = FastAPI()
    app.add_api_route("/api/chat_stream", endpoint, methods=["POST"])
    app.state.auth_manager = None
    with TestClient(app) as client:
        response = client.post("/api/chat_stream", json={"session": "session-1", "mode": "agent",
                               "message": "", "tool_approval_id": card.approval_id,
                               "tool_approval_decision": "approve_action"})
        assert response.status_code == 200, response.text
        grant = captured["exact_approval"]
        assert grant.scope is ToolApprovalScope.SINGLE_ACTION
        assert not grant.allow_remaining_actions and not grant.grants_chat_session and not grant.grants_workspace
        again = client.post("/api/chat_stream", json={"session": "session-1", "mode": "agent",
                            "tool_approval_id": card.approval_id, "tool_approval_decision": "approve_action"})
        assert again.status_code == 409


def test_resolved_single_action_history_does_not_grant_later_turns():
    from core.models import ChatMessage, Session
    card = pending(ToolApprovalStore()).public_payload()
    card["resolved"] = "approve_action"
    session = Session(id="session-1", name="Synthetic", endpoint_url="http://example.invalid", model="simulated",
                      history=[ChatMessage("assistant", "Permission requested", {"tool_events": [{"ask_user": card}]}),
                               ChatMessage("user", "Continue")])
    messages = session.get_context_messages()
    assert not any((m.get("metadata") or {}).get(CHAT_SESSION_APPROVAL_CONTEXT_MARKER) for m in messages)


@pytest.mark.parametrize("reason", ["owner", "session", "superseded", "expired"])
def test_http_single_action_rejects_foreign_and_stale_cards(monkeypatch, reason):
    import routes.chat_routes as route
    import time
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    store = ToolApprovalStore()
    monkeypatch.setattr(route, "tool_approval_store", store)
    card = pending(store, session_id="other" if reason == "session" else "session-1")
    if reason == "owner":
        monkeypatch.setattr(route, "effective_user", lambda request: "bob")
    elif reason == "superseded":
        pending(store)
    elif reason == "expired":
        monkeypatch.setattr(time, "time", lambda: card.expires_at + 1)
    app = FastAPI()
    app.add_api_route("/api/chat_stream", endpoint, methods=["POST"])
    app.state.auth_manager = None
    with TestClient(app) as client:
        response = client.post("/api/chat_stream", json={"session":"session-1", "message":"", "mode":"agent",
                               "tool_approval_id":card.approval_id, "tool_approval_decision":"approve_action"})
    assert response.status_code == 409, response.text
    assert "exact_approval" not in captured


def test_real_loop_executes_only_the_sealed_action_then_shows_new_card(monkeypatch):
    from src import agent_loop as loop, tool_execution as execution, tool_capabilities as caps
    store = ToolApprovalStore()
    card = pending(store, session_id="single-action-loop")
    grant = store.consume(card.approval_id, decision="approve_action", owner="alice", session_id="single-action-loop")
    dispatched = []
    calls = []
    async def implementation(block, **kwargs):
        dispatched.append((block.tool_type, block.content, kwargs.get("human_approved")))
        return block.tool_type, {"output": "Executed synthetic exact action", "exit_code": 0}
    async def model(*args, **kwargs):
        calls.append(1)
        yield "data: " + json.dumps({"type": "tool_calls", "calls": [{"name": "bash", "arguments": json.dumps({"command": "printf another"})}]}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(execution, "_execute_tool_block_impl", implementation)
    monkeypatch.setattr(loop, "stream_llm_with_fallback", model)
    monkeypatch.setattr(loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(loop, "_agent_route_tool_mode", lambda *a, **k: (True, False, True))
    monkeypatch.setattr(loop, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(loop, "_workspace_gate_granted", lambda *a: False)
    monkeypatch.setattr(loop, "get_setting", lambda key, default=None: {"agent_auto_continue_cycles": 0}.get(key, default))
    monkeypatch.setattr(caps, "tool_approval_mode", lambda: "ask")
    monkeypatch.setattr(caps, "_command_guard_denial", lambda *a, **k: None)
    import src.tool_index as index
    monkeypatch.setattr(index, "get_tool_index", lambda: None)
    async def run():
        return [chunk async for chunk in loop.stream_agent_loop(endpoint_url="http://127.0.0.1:1/v1/chat/completions",
            model="simulated", headers={}, owner="alice", session_id="single-action-loop", max_rounds=3,
            context_length=32768, exact_approval=grant, messages=[{"role": "user", "content": "Inspect the project"}],
            harness_options={"checkpoints": False, "run_tests": False, "repo_map": False})]
    output = "".join(asyncio.run(run()))
    assert dispatched == [("bash", "printf exact", True)], output
    assert calls
    assert '"kind": "tool_approval"' in output, output
    assert '"value": "approve_action"' in output
    assert "printf another" in output
