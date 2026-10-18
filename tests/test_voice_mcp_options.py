"""Native MCP start exposes the same optional context/wake options as the API."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("voice_mcp_options", Path(__file__).resolve().parents[1] / "mcp_servers/voice_server.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)


@pytest.mark.parametrize("options,hint,wake", [({}, "", False),
    ({"language_hint": "es", "wake_word": True}, "es", True),
    ({"language_hint": "en", "wake_word": False}, "en", False)])
def test_mcp_start_forwards_optional_context_and_wake(options, hint, wake):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "voice-1", "token": "private", "stream_url": "/stream"})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await voice._voice_session(client, {"action": "start", "session_id": "sid", **options})

    assert asyncio.run(scenario()) == {"id": "voice-1"}
    assert seen[0]["language"] == "auto" and seen[0]["language_hint"] == hint
    assert seen[0]["wake_word"] is wake and seen[0]["send_mode"] == "server"
    tool = next(t for t in voice.TOOLS if t.name == "voice_session")
    assert tool.inputSchema["properties"]["language_hint"]["enum"] == ["es", "en"]
    assert tool.inputSchema["properties"]["wake_word"]["type"] == "boolean"
