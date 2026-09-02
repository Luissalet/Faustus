"""The /agents payload (`delegate_tasks` form field) must reach
stream_agent_loop as harness_options["user_delegation"] — through the real
route, not a source grep. Ronda 6: the first cut assigned `_harness_options`
inside the streaming closure, which made it a closure-local and blew up
with "cannot access local variable '_harness_options'" on EVERY agent turn
in the live app; the unit tests were green."""
import json

import pytest

from tests.test_foreground_model_routing import _chat_stream_endpoint, _RouteRequest


@pytest.mark.asyncio
async def test_delegate_tasks_reach_the_loop_as_user_delegation(monkeypatch):
    captured = {}
    import routes.chat_routes as chat_routes
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    seen = {}
    real = chat_routes.stream_agent_loop

    async def spy(endpoint_url, model, messages, **kwargs):
        seen["harness_options"] = kwargs.get("harness_options")
        seen["messages"] = messages
        async for chunk in real(endpoint_url, model, messages, **kwargs):
            yield chunk

    monkeypatch.setattr(chat_routes, "stream_agent_loop", spy)
    req = _RouteRequest("agent")
    payload = {"tasks": [{"name": "a", "instruction": "[cart.py] add round_money(x)"},
                         {"name": "b", "instruction": "[tests/test_cart.py] add its test"}], "parallel": True}
    req._form["delegate_tasks"] = json.dumps(payload)
    response = await endpoint(req)
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else str(chunk).encode()
    assert b"Agent run failed" not in body and b"cannot access local variable" not in body
    ho = seen.get("harness_options") or {}
    assert ho.get("user_delegation") and ho["user_delegation"]["tasks"][0]["instruction"] == "[cart.py] add round_money(x)"
    # the model gets the explicit delegation instruction (the tool named once)
    last_user = [m for m in seen["messages"] if m.get("role") == "user"][-1]["content"]
    assert "delegate_agents" in last_user and "[cart.py] add round_money(x)" in last_user


@pytest.mark.asyncio
async def test_a_plain_agent_turn_still_streams(monkeypatch):
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, "agent", captured)
    response = await endpoint(_RouteRequest("agent"))
    body = b""
    async for chunk in response.body_iterator:
        body += chunk if isinstance(chunk, bytes) else str(chunk).encode()
    assert b"Agent run failed" not in body
    assert "agent" in captured
