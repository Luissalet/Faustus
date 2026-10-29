"""A timeout retires the expired relay channel; simulated loopback extension only."""
import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from src.browser_relay.protocol import EXTENSION_ID, ORIGIN
from src.browser_relay.transport import BrowserRelay


async def hello(relay):
    peer = await connect(f"ws://127.0.0.1:{relay.port}", origin=ORIGIN)
    await peer.send(json.dumps({"type": "hello", "protocol": 1, "extensionId": EXTENSION_ID}))
    ready = json.loads(await peer.recv())
    assert ready["type"] == "ready" and ready["connection_id"]
    return peer, ready["connection_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action,phase", [("screenshot", "not_dispatched"), ("navigate", "outcome_unknown")])
async def test_timeout_retires_original_channel_before_release_and_never_replays(action, phase):
    relay = await BrowserRelay(port=0, timeout=.06).start()
    peers, calls, requests = [], [], []
    try:
        peer, original_session = await hello(relay)
        peers.append(peer)
        healthy = asyncio.create_task(relay.call({"action": "tabs"}))
        calls.append(healthy)
        request = json.loads(await asyncio.wait_for(peer.recv(), .5))
        requests.append(request)
        await peer.send(json.dumps({"type": "result", "id": request["id"], "ok": True,
                                    "result": {"tabs": [{"id": 1, "active": True}]}, "phase": "readback_complete"}))
        assert (await healthy)["result"]["tabs"] == [{"id": 1, "active": True}]

        arguments = {"action": action, "tab_id": 1}
        if action == "navigate":
            arguments["url"] = "https://writer.test/next"
        blocked = asyncio.create_task(relay.call(arguments))
        calls.append(blocked)
        original = json.loads(await asyncio.wait_for(peer.recv(), .5))
        requests.append(original)
        assert original["arguments"] == arguments
        # This simulated extension deliberately never acknowledges the dispatched call.
        queued = asyncio.create_task(relay.call({"action": "release", "tab_id": 1}))
        calls.append(queued)
        await asyncio.sleep(0)  # Put it behind the actual relay serial lock.
        timed_out = await asyncio.wait_for(blocked, .8)
        assert timed_out["error"]["code"] == "command_timeout" and timed_out["phase"] == phase

        # Expiry retires the channel even while its browser command has no acknowledgement.
        await asyncio.wait_for(peer.wait_closed(), .3)
        assert peer.close_code == 1011 and peer.close_reason == "browser_relay_command_timeout"
        rejected = await asyncio.wait_for(queued, .5)
        assert rejected["error"]["code"] == "extension_disconnected"
        assert rejected["phase"] == "not_dispatched"
        # Buffered messages would be returned before recv raises the close, even for1011.
        with pytest.raises(ConnectionClosed):
            await peer.recv()
        assert len(requests) == 2

        async with asyncio.timeout(.5):
            while relay._peer is not None:
                await asyncio.sleep(.001)
        replacement, new_session = await hello(relay)
        peers.append(replacement)
        assert new_session != original_session
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(replacement.recv(), .02)
        fresh = asyncio.create_task(relay.call({"action": "tabs"}))
        calls.append(fresh)
        new_request = json.loads(await asyncio.wait_for(replacement.recv(), .5))
        assert new_request["arguments"] == {"action": "tabs"}
        assert new_request["id"] not in {row["id"] for row in requests}
        await replacement.send(json.dumps({"type": "result", "id": new_request["id"], "ok": True,
                                            "result": {"tabs": [{"id": 1, "active": True}]}, "phase": "readback_complete"}))
        assert (await fresh)["result"]["tabs"] == [{"id": 1, "active": True}]
    finally:
        for task in calls:
            if not task.done():
                task.cancel()
        await asyncio.gather(*calls, return_exceptions=True)
        for peer in peers:
            await peer.close()
        await relay.close()
    assert not relay._pending and relay._peer is None


@pytest.mark.asyncio
async def test_close_exception_keeps_original_mutation_timeout_phase(monkeypatch):
    relay = await BrowserRelay(port=0, timeout=.06).start()
    peer = None
    call = None
    try:
        peer, _ = await hello(relay)
        original_close = relay._peer.close

        async def closed_then_exception(*args, **kwargs):
            await original_close(*args, **kwargs)
            raise ConnectionClosed(None, None)

        monkeypatch.setattr(relay._peer, "close", closed_then_exception)
        call = asyncio.create_task(relay.call({"action": "navigate", "tab_id": 1, "url": "https://writer.test"}))
        request = json.loads(await asyncio.wait_for(peer.recv(), .5))
        assert request["arguments"]["action"] == "navigate"
        result = await asyncio.wait_for(call, .8)
        assert result["error"]["code"] == "command_timeout"
        assert result["phase"] == "outcome_unknown"
        await asyncio.wait_for(peer.wait_closed(), .3)
        assert not relay._ready
    finally:
        if call is not None and not call.done():
            call.cancel()
        if call is not None:
            await asyncio.gather(call, return_exceptions=True)
        if peer is not None:
            await peer.close()
        await relay.close()
    assert not relay._pending and relay._peer is None
