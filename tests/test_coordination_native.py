import asyncio
import json
from dataclasses import replace

import pytest

from src.coordination import runtime
from src.coordination.bindings import Binding, CoordinationError, OPERATIONS, StaleBinding, UnknownOutcome
from src.coordination.client import Client
from src.coordination.store import Store
from src.coordination.tools import NAMES


class Manager:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.after_call = None
        self.disabled = set()

    def get_all_tools(self):
        suffixes = {"inbox", "board", "task", "locks", "heartbeat", "task_claim", "checkpoint", "task_review", "post"}
        return [{"qualified_name": f"mcp__hub-a__hub_agora_{suffix}", "is_disabled": suffix in self.disabled}
                for suffix in suffixes]

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        suffix = name.split("hub_agora_")[1]
        result = self.responses.get(suffix, {"ok": True})
        await asyncio.sleep(0)
        if self.after_call:
            self.after_call()
        return result if "exit_code" in result else {"exit_code": 0, "structured_content": result}


@pytest.fixture
def native(tmp_path):
    store = Store(tmp_path)
    binding = store.save(Binding(id="b", owner="owner-a", project_id="p", session_id="s", connector_id="hub-a",
                                actor="faustus-native", operations=tuple(OPERATIONS), repos=("Writer",),
                                resources=("path:Writer/src",), thread_ids=(32,)))
    manager = Manager()
    client = Client(store, binding, manager, revalidate=lambda _: None)
    manager.responses["board"] = {"ok": True, "tasks": [{"id": 7, "repo": "Writer", "owner": "claude"}],
                                  "locks": [], "agents": [], "counts": {"review": 1}}
    return store, binding, manager, client


def task():
    return {"id": 7, "repo": "Writer", "paths": ["src/file.ts"], "owner": "faustus-native",
            "status": "in_progress", "submission_revision": 2, "latest_checkpoint": None}


@pytest.mark.asyncio
async def test_native_poll_compacts_and_persists_without_sync_or_ack(native):
    store, binding, manager, client = native
    manager.responses["inbox"] = {"ok": True, "messages": [
        {"id": 1, "thread_id": 32, "author": "claude", "body": "x" * 10000, "mentions": [binding.actor]},
        {"id": 2, "thread_id": 91, "author": "cursor", "body": "unrelated"}],
        "pending": [{"id": 500, "thread_id": 32, "author": "luis", "body": "an urgent mention", "mentions": [binding.actor]}]}
    first = await client.execute("poll", {})
    assert first["cursor"] == 2  # Pending mention500 must not skip unfetched3..499.
    assert {p["id"] for p in first["pending"]} == {1, 500}
    assert len(first["pending"][0]["body_excerpt"]) == 240
    assert len(json.dumps(first)) < 2000
    assert first["hub_acknowledged"] is False
    assert manager.calls[0] == ("mcp__hub-a__hub_agora_inbox", {"agent": binding.actor, "peek": True, "mine_only": False, "since_id": 0})
    manager.responses["inbox"] = {"ok": True, "messages": []}
    await client.execute("poll", {})
    assert manager.calls[2][1]["since_id"] == 2
    assert store.pending(binding)
    assert all(not name.endswith(("_sync", "_ack", "_heartbeat")) for name, _ in manager.calls)


@pytest.mark.asyncio
async def test_task_read_is_not_authority_until_native_delivery(native, monkeypatch):
    store, binding, manager, client = native
    manager.responses["task"] = {"ok": True, "task": task(), "messages": []}
    read = await client.execute("task", {"task_id": 7})
    with pytest.raises(CoordinationError, match="read this task"):
        await client.execute("checkpoint", {"task_id": 7, "expected_revision": 0, "payload": {"summary": "x"}})
    monkeypatch.setattr(runtime, "validate", lambda *a, **k: None)
    grant = runtime.Grant(store, binding, "owner-a", "s", "p", manager)
    grant.delivered(read)
    args = {"task_id": 7, "expected_revision": 0, "payload": {"summary": "x"}}
    assert grant.allows("coordination_checkpoint", args)
    await client.execute("checkpoint", args)
    assert manager.calls[-1][1] == {**args, "agent": binding.actor}


@pytest.mark.asyncio
async def test_detail_receipt_is_confirmed_only_by_delivery_hook(native, monkeypatch):
    store, binding, manager, client = native
    store.receive(binding, expected_cursor=0, next_cursor=1,
                  messages=[{"id": 1, "thread_id": 32, "body": "full detail"}])
    digest = store.pending(binding)[0]["digest"]
    fetched = await client.execute("detail", {"message_id": 1, "digest": digest})
    assert store.pending(binding)
    monkeypatch.setattr(runtime, "validate", lambda *a, **k: None)
    grant = runtime.Grant(store, binding, "owner-a", "s", "p", manager)
    grant.delivered({**fetched, "exit_code": 1})
    assert store.pending(binding)
    grant.delivered(fetched)
    assert store.pending(binding) == []
    assert manager.calls == []  # Stored detail read neither marks Hub seen nor sends a network call.


@pytest.mark.asyncio
async def test_unknown_write_outcome_is_not_retried_or_labeled_blocked(native):
    _, _, manager, client = native
    manager.responses["post"] = {"exit_code": 1, "outcome_unknown": True, "error": "lost reply"}
    with pytest.raises(UnknownOutcome):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_transport_failure_without_effect_receipt_cannot_authorize_write_retry(native):
    _, _, manager, client = native
    manager.responses["post"] = {"exit_code": 1, "stderr": "connection closed while waiting"}
    with pytest.raises(UnknownOutcome):
        await client.execute("post", {"thread_id": 32, "body": "publish once"})
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_binding_revoked_while_poll_awaits_does_not_advance_cursor(native):
    store, binding, manager, client = native
    manager.responses["inbox"] = {"ok": True, "messages": [{"id": 1, "thread_id": 32, "body": "x"}]}
    manager.after_call = lambda: store.save(replace(binding, active=False), expected_revision=1)
    with pytest.raises(StaleBinding):
        await client.execute("poll", {})
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM cursors").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM details").fetchone()[0] == 0
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_disabled_and_denied_underlying_connector_tools_cannot_be_wrapped(native):
    _, _, manager, client = native
    client.disabled_tools = {"hub_agora_post"}
    with pytest.raises(CoordinationError):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    client.disabled_tools = set()
    client.tool_policy = type("Policy", (), {"blocks": lambda self, name: name.endswith("_post")})()
    with pytest.raises(CoordinationError):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert manager.calls == []


@pytest.mark.asyncio
async def test_read_only_native_client_rejects_housekeeping_writes_before_network(native):
    _, _, manager, client = native
    client.read_only = True
    with pytest.raises(CoordinationError):
        await client.execute("heartbeat", {})
    with pytest.raises(CoordinationError):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert manager.calls == []


def test_native_schemas_registry_and_capabilities_match():
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_authority import AUTHORITY
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import ToolEffect, capabilities_for_tool
    schemas = {s["function"]["name"]: s for s in FUNCTION_TOOL_SCHEMAS}
    for name, action in NAMES.items():
        assert name in TOOL_HANDLERS and name in TOOL_TAGS and name in schemas
        spec = schemas[name]["function"]
        assert spec["parameters"]["additionalProperties"] is False
        assert not {"agent", "force", "exempt", "connector_id"} & set(spec["parameters"]["properties"])
        assert AUTHORITY.resolve(name).tool.canonical_id == f"coordination.{action}"
        caps = capabilities_for_tool(name)
        assert caps.known
        assert ToolEffect.USER_INTERACTION not in caps.effects
        assert (ToolEffect.WRITE_PRIVATE in caps.effects) == (action not in {"poll", "detail", "task", "board"})


def test_native_api_rejects_authority_fields_and_preserves_delivery_preconditions():
    from routes.coordination_routes import Action, Delivery, SaveBinding
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SaveBinding(connector_id="hub-a", owner="other")
    with pytest.raises(ValidationError):
        SaveBinding(connector_id="hub-a", expected_revision=True)
    with pytest.raises(ValidationError):
        Action(action="force")
    with pytest.raises(ValidationError):
        Action(action="poll", read_only="false")
    with pytest.raises(ValidationError):
        Delivery(receipt="r", binding_revision=True)


@pytest.mark.asyncio
async def test_write_revoked_after_dispatch_is_unknown_not_blocked(native):
    store, binding, manager, client = native
    manager.after_call = lambda: store.save(replace(binding, active=False), expected_revision=1)
    with pytest.raises(UnknownOutcome, match="may have completed"):
        await client.execute("post", {"thread_id": 32, "body": "x"})
    assert len(manager.calls) == 1


@pytest.mark.asyncio
async def test_lock_cannot_be_attached_to_a_foreign_task(native):
    store, binding, manager, client = native
    receipt = store.observe_task(binding, {**task(), "owner": "claude"})
    store.confirm_task_delivery(binding, receipt)
    with pytest.raises(CoordinationError, match="not owned"):
        await client.execute("lock", {"task_id": 7, "resources": ["path:Writer/src"]})
    assert not manager.calls


@pytest.mark.asyncio
async def test_review_needs_full_submission_discussion_not_a_task_summary(native, monkeypatch):
    store, binding, manager, client = native
    manager.responses["task"] = {"ok": True, "task": {**task(), "owner": "claude"},
        "messages": [{"id": 77, "thread_id": 90, "body": "Exact submitted work, evidence and limits", "author": "claude"}]}
    read = await client.execute("task", {"task_id": 7})
    monkeypatch.setattr(runtime, "validate", lambda *a, **k: None)
    grant = runtime.Grant(store, binding, "owner-a", "s", "p", manager)
    grant.delivered(read)
    vote = {"task_id": 7, "expected_submission_revision": 2, "verdict": "approve", "body": "reviewed"}
    with pytest.raises(CoordinationError, match="complete task discussion"):
        await client.execute("review", vote)
    assert not grant.allows("coordination_review", vote)
    pending = read["pending"][0]
    excerpt = await client.execute("detail", {"message_id": 77, "digest": pending["digest"], "length": 4})
    grant.delivered(excerpt)
    assert not grant.allows("coordination_review", vote)
    whole = await client.execute("detail", {"message_id": 77, "digest": pending["digest"], "length": 4000})
    grant.delivered(whole)
    assert grant.allows("coordination_review", vote)
    await client.execute("review", vote)
    assert manager.calls[-1][1]["expected_submission_revision"] == 2
