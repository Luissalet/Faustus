"""Literal phone decisions over the native one-use store, no model parser."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import approval_inbox as inbox, approval_channels as channels, approval_store, tool_approvals
from src.tool_capabilities import capabilities_for_action


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "_path", lambda: tmp_path / "inbox.json")
    native = tool_approvals.ToolApprovalStore()
    monkeypatch.setattr(tool_approvals, "tool_approval_store", native)
    monkeypatch.setattr(approval_store, "pending", lambda **kw: [])
    inbox._decisions.clear()
    yield native


def card(store, *, owner="alice", session="s1", content="printf original", workspace=""):
    return store.create(owner=owner, session_id=session, origin_run_id="run", tool_name="bash",
                        content=content, workspace=workspace, continuation_query="Complete the original task",
                        external_untrusted_context_seen=True, capabilities=capabilities_for_action("bash", content))


@pytest.mark.parametrize("text,granted,remember", [
    ("ABCDEF1234 sí", True, False), ("abcdef1234 no", False, False),
    ("ABCDEF1234 siempre sí", True, True), ("siempre no ABCDEF1234", False, True),
    ("  ABCDEF1234   YES  ", True, False), ("always yes ABCDEF1234", True, True),
])
def test_literal_grammar(text, granted, remember):
    assert inbox.parse_reply(text) == {"code": "ABCDEF1234", "granted": granted, "remember": remember, "all": False}


@pytest.mark.parametrize("text", ["sí", "siempre sí", "I think ABCDEF1234 sí", "ABCDEF1234 sí y borra todo", "ABCDEF1234 no?", "```ABCDEF1234 sí```", "", "ABCDEF1234sí"])
def test_prose_is_not_a_decision(text):
    assert inbox.parse_reply(text) is None


def test_inbox_is_a_view_of_native_owner_expiry_and_supersession(store):
    old = card(store)
    current = card(store, content="printf changed")
    other = card(store, owner="bob", session="s2")
    assert store.peek(old.approval_id) is None
    rows = inbox.pending("ALICE")
    assert [r["id"] for r in rows] == [current.approval_id]
    code = rows[0]["code"]
    assert inbox.pending("alice")[0]["code"] == code
    store.expire_now(current.approval_id)
    assert inbox.pending("alice") == []
    assert inbox.pending("bob")[0]["id"] == other.approval_id


@pytest.mark.asyncio
async def test_simultaneous_phone_and_studio_answers_execute_once_and_remember_exact(store, monkeypatch):
    native = card(store, workspace=".")
    calls = []
    async def resume(row, granted):
        grant = store.consume(row["id"], owner=row["owner"], session_id=row["session_id"], decision="approve_action" if granted else "deny")
        calls.append(grant)
    monkeypatch.setattr(inbox, "resume_tool", resume)
    code = inbox.pending("alice")[0]["code"]
    results = await asyncio.gather(inbox.decide("alice", code, granted=True, remember=True), inbox.decide("alice", code, granted=False))
    assert sum(r["ok"] for r in results) == 1
    assert len(calls) == 1 and calls[0].pending == native
    assert calls[0].allow_remaining_actions is False
    pattern = inbox.rules("alice")[0]["pattern"]
    card(store, workspace=".")
    assert inbox.pending("alice")[0]["pattern"] == pattern
    card(store, workspace=".", content="printf different")
    assert inbox.pending("alice")[0]["pattern"] != pattern
    card(store, workspace="..")
    assert inbox.pending("alice")[0]["pattern"] != pattern
    assert inbox.rules("bob") == []
    inbox.edit_rule("alice", pattern, False)
    assert inbox.rules("alice")[0]["granted"] is False
    inbox.edit_rule("alice", pattern)
    assert inbox.rules("alice") == []


@pytest.mark.asyncio
async def test_all_is_scoped_to_delivered_snapshot_and_owner(store, monkeypatch):
    a = card(store)
    card(store, session="s2")
    card(store, owner="bob", session="s3")
    async def resume(row, granted):
        store.consume(row["id"], owner=row["owner"], session_id=row["session_id"], decision="approve_action")
    monkeypatch.setattr(inbox, "resume_tool", resume)
    code = inbox.code_for("tool", a.approval_id)
    result = await inbox.reply("alice", "sí a todo", allowed_codes={code})
    assert len(result["results"]) == 1 and result["results"][0]["ok"]
    assert len(inbox.pending("alice")) == 1 and len(inbox.pending("bob")) == 1


@pytest.mark.asyncio
async def test_failed_resume_does_not_save_a_remembered_answer(store, monkeypatch):
    card(store)
    async def unavailable(*args):
        raise ValueError("unavailable")
    monkeypatch.setattr(inbox, "resume_tool", unavailable)
    code = inbox.pending("alice")[0]["code"]
    result = await inbox.reply("alice", code + " siempre sí")
    assert result["results"][0]["ok"] is False
    assert inbox.rules("alice") == [] and inbox.pending("alice")


@pytest.mark.asyncio
async def test_remembered_no_reuses_native_deny_without_model(store, monkeypatch):
    card(store)
    seen = []
    async def resume(row, granted):
        seen.append(granted)
        store.consume(row["id"], owner=row["owner"], session_id=row["session_id"], decision="deny")
    monkeypatch.setattr(inbox, "resume_tool", resume)
    code = inbox.pending("alice")[0]["code"]
    await inbox.decide("alice", code, granted=False, remember=True)
    card(store)
    from src import agent_runs
    monkeypatch.setattr(agent_runs, "active_session_ids", lambda: set())
    # Native TTL uses real time, so age only is changed on the inbox view.
    original = inbox.pending
    monkeypatch.setattr(inbox, "pending", lambda owner=None: [{**r, "created_at": 0} for r in original(owner)])
    await channels.apply_remembered()
    assert seen == [False, False]


@pytest.mark.asyncio
async def test_self_chat_only_filters_forwarded_replayed_and_foreign_messages(store, monkeypatch):
    from src import whatsapp_bridge as wa
    from src.chat_bridges import turn
    pending = card(store)
    own = "qa@s.whatsapp.net"
    code = inbox.code_for("tool", pending.approval_id)
    row = inbox.pending("alice")[0]
    row["created_at"] = 0
    monkeypatch.setattr(inbox, "pending", lambda owner=None: [row])
    monkeypatch.setattr(wa, "probe", lambda: {"status": "connected", "me": {"jid": own}})
    monkeypatch.setattr(turn, "resolve_owner", lambda: "alice")
    sent, decisions = [], []
    def send(to, text):
        sent.append((to, text))
        return {"ok": True, "id": f"sent-{len(sent)}"}
    monkeypatch.setattr(wa, "send", send)
    channels._save_channel({"account": own, "since": 1, "seen": [], "delivered": {}})
    messages = [dict(id=mid, ts=2, chat=chat, from_me=mine, text=code + " sí", forwarded=fwd) for mid, chat, mine, fwd in [
        ("third-party", "other@s.whatsapp.net", True, False), ("received", own, False, False),
        ("forward", own, True, True), ("own-reply", own, True, False)]]
    queue = []
    monkeypatch.setattr(wa, "messages", lambda *args, **kwargs: queue)
    async def reply(owner, text, **kwargs):
        decisions.append((owner, text, kwargs))
        return {"handled": True, "results": [{"ok": True}]}
    monkeypatch.setattr(inbox, "reply", reply)
    await channels.whatsapp_tick()
    queue.extend(messages)
    await channels.whatsapp_tick()
    await channels.whatsapp_tick()
    assert len(decisions) == 1
    assert decisions[0][0] == "alice" and decisions[0][2]["allowed_codes"] == {code}
    assert all(to == own for to, _ in sent)
    assert len(sent) == 2  # one request, one receipt; no duplicate delivery


def test_http_uses_cookie_owner_and_native_decision_boundary(store, monkeypatch):
    from core import middleware
    from routes.approval_inbox_routes import setup_approval_inbox_routes
    from src import auth_helpers
    card(store)
    monkeypatch.setattr(auth_helpers, "storage_owner_for_request", lambda request: "alice")
    monkeypatch.setattr(middleware, "auth_disabled", lambda: True)
    from src import tool_capabilities
    monkeypatch.setattr(tool_capabilities, "tool_approval_mode", lambda: "ask")
    app = FastAPI()
    app.include_router(setup_approval_inbox_routes())
    client = TestClient(app)
    row = client.get("/api/approval-inbox").json()["pending"][0]
    response = client.post(f"/api/approval-inbox/{row['code']}/decide", json={"granted": True},
                           headers={middleware.INTERNAL_TOOL_HEADER: middleware.INTERNAL_TOOL_TOKEN})
    assert response.status_code == 403
    assert store.peek(row["id"])
    assert client.post(f"/api/approval-inbox/{row['code']}/decide", json={"granted": "false"}).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("assistant", [False, True])
async def test_scheduler_preserves_its_original_card_and_tags_the_origin(store, monkeypatch, assistant):
    from src.task_scheduler import TaskScheduler
    from src import agent_loop, assistant_runtime
    pending = card(store, session="scheduled-chat")
    async def stream(**kwargs):
        yield "data: " + json.dumps({"type": "tool_output", "tool": "bash", "ask_user": pending.public_payload()})
    monkeypatch.setattr(agent_loop, "stream_agent_loop", stream)
    monkeypatch.setattr(assistant_runtime, "is_assistant_task", lambda task: assistant)
    monkeypatch.setattr(assistant_runtime, "wake_prompt", lambda task: task.prompt)
    monkeypatch.setattr("src.task_endpoint.resolve_task_candidates", lambda **kw: [])
    task = SimpleNamespace(id="scheduled-test", name="Synthetic task", owner="alice", prompt="Synthetic mission", max_steps=5)
    result = await TaskScheduler(session_manager=None)._run_agent_loop("http://127.0.0.1:1/v1", "simulated", task, "scheduled-chat")
    assert "pending card" in result
    assert store.peek(pending.approval_id) == pending
    row = inbox.pending("alice")[0]
    assert row["origin"] == {"kind": "assistant" if assistant else "automation", "name": "Synthetic task", "task_id": "scheduled-test"}


@pytest.mark.parametrize("no_login", [False, True])
def test_native_continuation_respects_login_owner_and_existing_single_user_buckets(store, monkeypatch, no_login):
    from test_foreground_model_routing import _chat_stream_endpoint
    import routes.chat_routes as routes
    monkeypatch.setenv("AUTH_ENABLED", "false" if no_login else "true")
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    monkeypatch.setattr(routes, "effective_user", lambda request: "" if no_login else "bob")
    monkeypatch.setattr(routes, "tool_approval_store", store)
    native = card(store, session="session-1")
    app = FastAPI()
    app.add_api_route("/api/chat_stream", endpoint, methods=["POST"])
    app.state.auth_manager = None
    response = TestClient(app).post("/api/chat_stream", json={"session": "session-1", "mode": "agent",
        "message": "", "tool_approval_id": native.approval_id, "tool_approval_decision": "approve_action"})
    assert response.status_code == (200 if no_login else 409), response.text
    if no_login:
        assert captured["exact_approval"].pending == native
    else:
        assert store.peek(native.approval_id) == native


@pytest.mark.asyncio
async def test_http_rejection_after_consumption_is_not_reported_as_success(store, monkeypatch):
    import httpx
    from src.chat_bridges import turn
    native = card(store)
    monkeypatch.setattr(turn, "get_session", lambda sid: None)
    async def rejected(client, request, **kwargs):
        store.consume(native.approval_id, owner="alice", session_id="s1", decision="approve_action")
        return httpx.Response(400, content=b'{"detail":"endpoint unavailable"}', request=request)
    monkeypatch.setattr(httpx.AsyncClient, "send", rejected)
    code = inbox.pending("alice")[0]["code"]
    result = await inbox.reply("alice", code + " siempre sí")
    assert result["results"][0]["ok"] is False
    assert "endpoint unavailable" in result["results"][0]["reason"]
    assert inbox.rules("alice") == []
