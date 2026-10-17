"""Participant overrides must survive storage and reach the ordinary chat endpoint."""
import pytest
from fastapi import FastAPI
from starlette.responses import StreamingResponse
from src.group_orchestrator import NativeGroupBridge, GroupCoordinator, GroupStore, participant_execution_options
from test_group_native_bridge import Sessions, turn, step
from test_group_membership import Sessions as CreatableSessions


@pytest.mark.parametrize("settings,expected", [
    ({"reasoningEffort": "high", "thinkingEnabled": True}, {"reasoning_effort": "high", "thinking_enabled": True}),
    ({"thinkingEnabled": False}, {"thinking_enabled": False}),
    ({}, {}),
])
async def test_each_member_sends_its_own_native_preferences(monkeypatch, settings, expected):
    from src import chat_outbox
    monkeypatch.setattr(chat_outbox, "get", lambda **kw: None)
    seen = []
    async def endpoint(request, initial_response_timeout_s=None):
        seen.append(await request.json())
        async def stream():
            yield 'data: {"delta":"Actual answer"}\n\n'
            yield 'data: [DONE]\n\n'
        return StreamingResponse(stream(), headers={"X-Odysseus-Run-Id": "qa-reasoning"})
    bridge = NativeGroupBridge(Sessions(), endpoint, FastAPI())
    t = turn()
    member = {**t["definition"]["participants"][0], **settings}
    async def progress(*args): pass
    await bridge.invoke(turn=t, step=step(), member=member, owner="owner", progress=progress)
    actual = {k: seen[0][k] for k in ("reasoning_effort", "thinking_enabled") if k in seen[0]}
    assert actual == expected


def test_preferences_survive_create_update_and_reopening(tmp_path):
    bridge = NativeGroupBridge(CreatableSessions(), None, None)
    path = tmp_path / "groups.db"
    driver = GroupCoordinator(None, bridge, store=GroupStore(path))
    participants = [{"route": {"model": "fixture", "endpointUrl": "http://127.0.0.1:65534"}, **choice}
                    for choice in ({"reasoningEffort": "max", "thinkingEnabled": True}, {"thinkingEnabled": False})]
    group = driver.adopt(bridge.create(participants, "parallel", "alice"), "alice")
    changed = [{**group["participants"][0], "reasoningEffort": "low"}, group["participants"][1]]
    updated = driver.update(group["parentId"], "alice", {"participants": changed}, group["revision"])
    restored = GroupStore(path).group(group["parentId"], "alice")
    assert restored["participants"][0]["reasoningEffort"] == "low"
    assert restored["participants"][1]["thinkingEnabled"] is False
    assert [m["sessionId"] for m in updated["participants"]] == [m["sessionId"] for m in group["participants"]]


def test_invalid_settings_are_validated_by_the_existing_preferences_contract():
    with pytest.raises(ValueError):
        participant_execution_options({"reasoningEffort": "invented"})
    with pytest.raises(ValueError):
        participant_execution_options({"thinkingEnabled": "false"})
