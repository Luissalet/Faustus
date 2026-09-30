"""`generated_image` crosses the `/api/chat_stream` allowlist in agent mode.

Seen live (30-09-2026, Prospero edit through the browser): the agent loop
logged the event, the chat showed no image until the page was reloaded — the
route's allowlist did not name the type. Same route harness as
`tests/test_l40_capabilities_changed_route.py`.
"""
import json

import pytest

from tests.test_l40_capabilities_changed_route import _RouteRequest, _chat_stream_endpoint


@pytest.mark.asyncio
async def test_generated_image_event_reaches_the_client():
    monkeypatch = pytest.MonkeyPatch()
    try:
        image_chunk = 'data: ' + json.dumps({
            "type": "generated_image",
            "url": "/api/generated-image/3e3714e7-be03-4b8a-99b1-129a64e5008c.png",
            "image_id": "3e3714e7-be03-4b8a-99b1-129a64e5008c",
        }) + '\n\n'
        agent_chunks = [
            'data: ' + json.dumps({"type": "tool_output", "tool": "edit_image", "output": "ok"}) + '\n\n',
            image_chunk,
            'data: ' + json.dumps({"delta": "Listo."}) + '\n\n',
            "data: [DONE]\n\n",
        ]
        endpoint = _chat_stream_endpoint(monkeypatch, agent_chunks)
        # A session of its own: the detached-run registry keeps the other
        # route test's finished run (and its event loop) under its id.
        import routes.chat_routes as chat_routes
        monkeypatch.setattr(chat_routes, "coerce_message_and_session",
                            lambda *a, **k: ("hello", "session-generated-image"))
        response = await endpoint(_RouteRequest())
        emitted = [chunk async for chunk in response.body_iterator]
    finally:
        monkeypatch.undo()
    decoded = [json.loads(chunk[len("data: "):]) for chunk in emitted
               if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]")]
    expected = json.loads(image_chunk[len("data: "):])
    assert any(expected.items() <= d.items() for d in decoded), emitted


def test_every_loop_event_studio_decodes_is_forwarded_by_the_route():
    """The loop's event types that Studio's decoder handles must be in the
    route's forwarding switch (the gap that hid generated images)."""
    import io
    import re
    loop = io.open("src/agent_loop.py", encoding="utf-8").read()
    route = io.open("routes/chat_routes.py", encoding="utf-8").read()
    front = io.open("studio/src/adapters/chat.ts", encoding="utf-8").read()
    emitted = set(re.findall(r'["\']type["\']\s*:\s*["\']([a-z_]+)["\']', loop))
    start = route.rindex("(", 0, route.index('"tool_start", "tool_output", "agent_step"'))
    lines, depth = [], 0
    for line in route[start:].splitlines():
        code = line.split("#")[0]
        lines.append(code)
        depth += code.count("(") - code.count(")")
        if depth <= 0:
            break
    allow = set(re.findall(r'"([a-z_]+)"', "\n".join(lines)))
    handled = set(re.findall(r'data\.get\("type"\) == "([a-z_]+)"', route))
    decode = front[front.index("export function decode("):front.index("async function* streamEvents")]
    decoded = set(re.findall(r"case '([a-z_]+)':", decode))
    missing = (emitted & decoded) - allow - handled
    assert not missing, sorted(missing)
