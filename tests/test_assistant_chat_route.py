"""Regression: ordinary Chat bypasses the agent prompt builder."""
import asyncio
import copy
import json

import pytest

from tests.test_personal_assistants import database
from tests.test_lote20_chat_routes_budget_exhausted import _chat_stream_endpoint, _RouteRequest


@pytest.mark.parametrize("no_skills", [False, True])
@pytest.mark.parametrize("skills_enabled", [False, True])
def test_plain_chat_receives_real_profile_and_respects_skill_controls(database, monkeypatch, no_skills, skills_enabled):
    import routes.chat_routes as routes
    from routes import prefs_routes
    from core.database import Session
    from src import personal_assistants, assistant_runtime

    sid = "session-budget-exhausted"
    with database() as db:
        db.add(Session(id=sid, name="Profile Chat", endpoint_url="", model="", owner="alice"))
        db.commit()
    personal_assistants.save(sid, "Helper", "", "alice", instructions="PLAIN-CHAT-ROLE", skills=["check"])
    monkeypatch.setattr(assistant_runtime, "skill_catalog", lambda owner: [{"name": "check", "procedure": ["PLAIN-CHAT-SKILL"]}])
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: {"skills_enabled": skills_enabled})
    endpoint = _chat_stream_endpoint(monkeypatch, [])
    monkeypatch.setattr(routes, "get_session_mode", lambda sid: "chat")
    seen = []
    async def stream(candidates, messages, **kwargs):
        seen.append(copy.deepcopy(messages))
        yield 'data: ' + json.dumps({"delta": "Configured answer"}) + '\n\n'
        yield 'data: {"type":"finish","finish_reason":"stop"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(routes, "stream_llm_with_fallback", stream)
    async def turn():
        request = _RouteRequest()
        request._form.update(mode="chat", no_skills=str(no_skills).lower())
        response = await endpoint(request)
        return [chunk async for chunk in response.body_iterator]
    asyncio.run(turn())
    assert len(seen) == 1
    assert "PLAIN-CHAT-ROLE" in str(seen[0])
    assert ("PLAIN-CHAT-SKILL" in str(seen[0])) == (skills_enabled and not no_skills)
    assert seen[0][-1]["content"] == "hello"
    # Configuration edits are read next turn, independently of route caches.
    personal_assistants.save(sid, "Helper", "", "alice", instructions="EDITED-CHAT-ROLE")
    asyncio.run(turn())
    assert "EDITED-CHAT-ROLE" in str(seen[-1])
    assert "PLAIN-CHAT-ROLE" not in str(seen[-1])
