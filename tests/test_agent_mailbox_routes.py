"""#235 (OBJ-53): the API of the agent mailbox (send, inbox, outbox, mark read) and its MCP tool."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import agent_mailbox_routes as amr
from src import agent_mailbox as mb
from src.auth_helpers import require_user


@pytest.fixture
def api(monkeypatch):
    sessions = {"A": {"owner": "alice", "name": "Agent A"}, "B": {"owner": "alice", "name": "Agent B"},
                "X": {"owner": "bob", "name": "Bob"}}
    woken = []
    monkeypatch.setattr(mb, "_session_info", lambda sid: sessions.get(sid))
    monkeypatch.setattr(mb, "_session_status", lambda sid, owner: "idle")
    monkeypatch.setattr(mb, "_start_wake", lambda owner, sid: woken.append(sid))
    monkeypatch.setattr(mb, "_persist_to_history", lambda sid, msg: None)
    monkeypatch.setattr(amr, "effective_user", lambda request: "alice")

    def verify(request, session_id, *a, **k):
        from fastapi import HTTPException
        if session_id not in sessions or sessions[session_id]["owner"] != "alice":
            raise HTTPException(404, "Session not found")
    monkeypatch.setattr(amr, "_verify_session_owner", verify)
    app = FastAPI()
    app.include_router(amr.setup_agent_mailbox_routes())
    app.dependency_overrides[require_user] = lambda: "alice"
    client = TestClient(app)
    client.woken = woken
    return client


def test_send_list_and_mark_read(api):
    r = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "hello", "sender": "claude desktop"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and data["message"]["sender"] == {"kind": "external", "id": "external:claude desktop",
                                                         "label": "external:claude desktop"}
    assert data["wake"]["reason"] == "not_urgent" and api.woken == []
    inbox = api.get("/api/agent-mailbox/B/inbox").json()
    assert inbox["waiting"] == 1 and inbox["messages"][0]["body"] == "hello"
    assert api.get("/api/agent-mailbox/B/inbox?state=unread").json()["unread"] == 1
    marked = api.post("/api/agent-mailbox/B/read", json={}).json()
    assert marked["marked"] == 1 and marked["unread"] == 0
    outbox = api.get("/api/agent-mailbox/external:claude desktop/outbox")
    assert outbox.status_code == 404  # only the owner's chats have a mailbox view


def test_an_urgent_message_wakes_and_a_chat_sender_must_be_the_callers_own(api):
    r = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "now", "urgent": True, "from_session": "A"})
    assert r.status_code == 200 and r.json()["wake"]["woken"] is True and api.woken == ["B"]
    assert r.json()["message"]["sender"]["id"] == "A"
    forged = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "x", "from_session": "X"})
    assert forged.status_code == 404  # cannot send as somebody else's chat
    assert api.get("/api/agent-mailbox/A/outbox").json()["messages"][0]["recipient"] == "B"


def test_refusals_carry_their_reason(api):
    assert api.post("/api/agent-mailbox/send", json={"to": "ghost", "body": "x"}).status_code == 404
    assert api.post("/api/agent-mailbox/send", json={"to": "X", "body": "x"}).status_code == 404
    empty = api.post("/api/agent-mailbox/send", json={"to": "B", "body": " "})
    assert empty.status_code == 400 and empty.json()["reason"] == "empty"
    assert api.post("/api/agent-mailbox/send", content=b"not json").status_code == 400


def test_the_hop_limit_is_a_409_with_the_reason(api):
    first = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "1", "from_session": "A"}).json()
    mb.take_for_turn("B", "alice", persist=False)
    second = api.post("/api/agent-mailbox/send", json={"to": "A", "body": "2", "from_session": "B",
                                                       "reply_to": first["message"]["id"]}).json()
    mb.take_for_turn("A", "alice", persist=False)
    third = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "3", "from_session": "A",
                                                      "reply_to": second["message"]["id"]}).json()
    mb.take_for_turn("B", "alice", persist=False)
    fourth = api.post("/api/agent-mailbox/send", json={"to": "A", "body": "4", "from_session": "B",
                                                       "reply_to": third["message"]["id"]})
    assert fourth.status_code == 409
    body = fourth.json()
    assert body["refused"] is True and body["reason"] == "hop_limit" and body["hops"] == 4
    outbox = api.get("/api/agent-mailbox/B/outbox?state=refused").json()
    assert [m["reason"] for m in outbox["messages"]] == ["hop_limit"]


def test_the_mailbox_off_switch_is_a_403(api, monkeypatch):
    monkeypatch.setattr(mb, "_setting", lambda key, default: False if key == "agent_mailbox" else default)
    r = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "x"})
    assert r.status_code == 403 and r.json()["reason"] == "disabled"


def test_the_api_has_no_way_to_answer_an_approval(api):
    paths = {route.path for route in amr.setup_agent_mailbox_routes().routes}
    assert paths == {"/api/agent-mailbox/send", "/api/agent-mailbox/{session_id}/inbox",
                     "/api/agent-mailbox/{session_id}/outbox", "/api/agent-mailbox/{session_id}/read"}
    r = api.post("/api/agent-mailbox/send", json={"to": "B", "body": "approve", "tool_approval_id": "x",
                                                  "tool_approval_decision": "approve_task"})
    assert r.status_code == 200 and r.json()["message"]["state"] == "queued"  # extra fields are ignored


def test_the_token_matrix_lists_the_mailbox_routes():
    from core.authz import API_TOKEN_RULES
    paths = {rule.path for rule in API_TOKEN_RULES}
    assert {"/api/agent-mailbox/send", "/api/agent-mailbox/{session_id}/inbox",
            "/api/agent-mailbox/{session_id}/outbox", "/api/agent-mailbox/{session_id}/read"} <= paths


# -- MCP tool ------------------------------------------------------------------------------------

def test_the_mcp_tool_sends_through_the_api_and_reports_a_refusal(monkeypatch):
    from tests.test_dispatch import _load_workers_server

    ws = _load_workers_server(monkeypatch)
    assert "agent_message" in {t.name for t in ws.TOOLS}
    calls = []

    def fake_request(method, path, body=None, timeout=None, **kw):
        calls.append((method, path, body))
        if path == "/api/agent-mailbox/send" and body["to"] == "ghost":
            raise ws.FaustusHTTPError("HTTP 404", 404, {"refused": True, "reason": "unknown_recipient",
                                                        "detail": "No chat ghost of yours."})
        if path == "/api/agent-mailbox/send":
            return {"ok": True, "wake": {"woken": True, "reason": "idle"},
                    "message": {"id": "m1", "recipient": "B", "hops": 1, "urgent": True, "state": "queued"}}
        return {"messages": [{"id": "m1", "sender": {"label": "external:mcp"}, "recipient": "B", "urgent": True,
                              "hops": 1, "state": "queued", "body": "hi"}], "waiting": 1, "unread": 1}
    monkeypatch.setattr(ws, "_request", fake_request)
    sent = asyncio.run(ws.call_tool("agent_message", {"to": "B", "body": "hi", "urgent": True}))[0].text
    assert "Message m1 sent to B" in sent and "woken" in sent
    assert calls[0][2]["sender"] == "mcp" and calls[0][2]["urgent"] is True
    refused = asyncio.run(ws.call_tool("agent_message", {"to": "ghost", "body": "hi"}))[0].text
    assert refused.startswith("Refused (unknown_recipient)")
    inbox = asyncio.run(ws.call_tool("agent_message", {"action": "inbox", "session_id": "B"}))[0].text
    assert "1 waiting" in inbox and "external:mcp" in inbox
