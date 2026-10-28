"""Actual stdio MCP child + actual WS, with an explicitly simulated extension."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

import pytest
from websockets.asyncio.client import connect

from src.browser_relay.protocol import EXTENSION_ID, ORIGIN

ROOT = Path(__file__).resolve().parents[2]
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg=="


class StdioClient:
    def __init__(self, process):
        self.process = process
        self.next_id = 0
        self.stderr = []

    async def rpc(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}}) + "\n").encode())
        await self.process.stdin.drain()
        while True:
            line = await asyncio.wait_for(self.process.stdout.readline(), 10)
            assert line, f"MCP ended: {self.stderr}"
            value = json.loads(line)
            if value.get("id") == ident:
                assert "error" not in value, value
                return value["result"]

    async def call(self, arguments, name="browser_session"):
        return await self.rpc("tools/call", {"name": name, "arguments": arguments})


@pytest.mark.asyncio
async def test_real_stdio_catalog_validation_actions_image_and_eof():
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / "src/browser_relay/server.py"), "--port", "0", "--timeout", ".3",
                                                  cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    client = StdioClient(process)
    async def stderr_tail():
        while line := await process.stderr.readline():
            client.stderr.append(line.decode().rstrip())
    drain = None
    peer = None
    try:
        line = (await asyncio.wait_for(process.stderr.readline(), 10)).decode().strip()
        assert line.startswith("browser_relay_listening=127.0.0.1:"), line
        port = int(line.rsplit(":", 1)[1])
        assert port != 7016
        drain = asyncio.create_task(stderr_tail())
        initial = await client.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "explicit-simulated-extension-test", "version": "1"}})
        assert initial["serverInfo"]["name"] == "browser_session"
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await process.stdin.drain()
        catalog = await client.rpc("tools/list")
        assert len(catalog["tools"]) == 1
        tool = catalog["tools"][0]
        assert tool["annotations"]["readOnlyHint"] is False and tool["annotations"]["idempotentHint"] is False
        assert tool["inputSchema"]["additionalProperties"] is False
        absent = await client.call({"action": "tabs"})
        assert absent["isError"] and absent["structuredContent"]["error"]["code"] == "extension_unavailable"
        peer = await connect(f"ws://127.0.0.1:{port}", origin=ORIGIN)
        await peer.send(json.dumps({"type": "hello", "protocol": 1, "extensionId": EXTENSION_ID}))
        assert json.loads(await peer.recv())["type"] == "ready"
        invalid = await client.call({"action": "click", "tab_id": 3})
        assert invalid["isError"] and invalid["structuredContent"]["phase"] == "not_dispatched"
        for arguments in [
            {"action": "tabs"}, {"action": "snapshot", "tab_id": 3},
            {"action": "click", "tab_id": 3, "ref": "g1:r1"},
            {"action": "type", "tab_id": 3, "ref": "g1:r1", "text": "Élodie 李明 Ω"},
            {"action": "press", "tab_id": 3, "key": "Enter", "ref": "g1:r1"},
            {"action": "navigate", "tab_id": 3, "url": "https://example.test/é"},
            {"action": "screenshot", "tab_id": 3}, {"action": "release", "tab_id": 3},
        ]:
            call = asyncio.create_task(client.call(arguments))
            command = json.loads(await asyncio.wait_for(peer.recv(), 5))
            assert command["type"] == "command" and command["arguments"] == arguments
            result = {"tab": {"id": 3}, "snapshot": {"scope": "main_frame"}}
            if arguments["action"] == "tabs":
                result = {"tabs": [{"id": 3, "title": "Simulated Élodie", "url": "https://example.test", "active": False, "window_id": 1}]}
            elif arguments["action"] == "screenshot":
                result = {"tab": {"id": 3}, "image": {"data": PNG, "mimeType": "image/png"}}
            await peer.send(json.dumps({"type": "result", "id": command["id"], "ok": True, "result": result, "phase": "readback_complete"}))
            response = await call
            assert not response["isError"]
            if arguments["action"] == "screenshot":
                assert response["content"][1] == {"type": "image", "data": PNG, "mimeType": "image/png"}
                assert base64.b64decode(response["content"][1]["data"]).startswith(b"\x89PNG")
                assert PNG not in response["content"][0]["text"] and "image" not in response["structuredContent"]["result"]
        call = asyncio.create_task(client.call({"action": "screenshot", "tab_id": 3}))
        command = json.loads(await peer.recv())
        await peer.send(json.dumps({"type": "result", "id": command["id"], "ok": True,
                                   "result": {"tab": {"id": 3}, "image": {"data": "invalid", "mimeType": "image/png"}}, "phase": "readback_complete"}))
        bad_image = await call
        assert bad_image["isError"] and bad_image["structuredContent"]["error"]["code"] == "invalid_screenshot"
        assert bad_image["structuredContent"]["phase"] == "not_dispatched" and len(bad_image["content"]) == 1
        call = asyncio.create_task(client.call({"action": "click", "tab_id": 3, "ref": "old:g1:r1"}))
        command = json.loads(await peer.recv())
        await peer.send(json.dumps({"type": "result", "id": command["id"], "ok": False, "error": {"code": "stale_ref", "message": "Snapshot generation ended"}, "phase": "not_dispatched"}))
        stale = await call
        assert stale["isError"] and stale["structuredContent"]["error"]["code"] == "stale_ref"
        process.stdin.close()
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0
        await drain
        assert not client.stderr, client.stderr
        assert await process.stdout.read() == b""
        await asyncio.wait_for(peer.wait_closed(), 5)
    finally:
        if peer is not None:
            await peer.close()
        if process.returncode is None:
            process.kill()
            await process.wait()
        if drain is not None:
            await drain
