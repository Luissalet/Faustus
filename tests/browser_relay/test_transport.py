"""Real loopback WebSockets with an explicitly simulated extension, never Brave."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from src.browser_relay.protocol import EXTENSION_ID, ORIGIN
from src.browser_relay.transport import BrowserRelay


@asynccontextmanager
async def running_relay(**kwargs):
    relay = await BrowserRelay(port=0, **kwargs).start()
    try:
        yield relay
    finally:
        await relay.close()
    assert not relay._pending and relay._peer is None


async def hello(relay):
    peer = await connect(f"ws://127.0.0.1:{relay.port}", origin=ORIGIN)
    await peer.send(json.dumps({"type": "hello", "protocol": 1, "extensionId": EXTENSION_ID}))
    ready = json.loads(await peer.recv())
    assert ready["type"] == "ready" and ready["connection_id"]
    return peer


async def reply(peer, request, result=None, **extra):
    await peer.send(json.dumps({"type": "result", "id": request["id"], "ok": True,
                               "result": result or {"tab": {"id": request["arguments"].get("tab_id")}},
                               "phase": "readback_complete", **extra}))


@pytest.mark.asyncio
async def test_no_connection_and_invalid_arguments_never_dispatch():
    async with running_relay() as relay:
        assert (await relay.call({"action": "tabs"}))["error"]["code"] == "extension_unavailable"
        for args in ({"action": "click", "tab_id": 1}, {"action": "type", "tab_id": 1, "ref": "r"},
                     {"action": "click", "tab_id": True, "ref": "r"}, {"action": "snapshot", "tab_id": "1"},
                     {"action": "tabs", "extra": 1}, {"action": "click", "tab_id": 1, "ref": ""},
                     {"action": "other"}, None):
            result = await relay.call(args)
            assert result["error"]["code"] == "invalid_arguments" and result["phase"] == "not_dispatched"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "http://127.0.0.1", ORIGIN + "/", "chrome-extension://different"])
async def test_exact_origin(origin):
    async with running_relay() as relay:
        with pytest.raises(InvalidStatus) as exc:
            async with connect(f"ws://127.0.0.1:{relay.port}", origin=origin):
                pytest.fail("Foreign origin accepted")
        assert exc.value.response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [{"type": "hello", "protocol": 2, "extensionId": EXTENSION_ID},
                                      {"type": "hello", "protocol": True, "extensionId": EXTENSION_ID},
                                      {"type": "hello", "protocol": 1, "extensionId": "wrong"}, []])
async def test_protocol_rejected(message):
    async with running_relay() as relay:
        async with connect(f"ws://127.0.0.1:{relay.port}", origin=ORIGIN) as peer:
            await peer.send(json.dumps(message))
            with pytest.raises(ConnectionClosed) as exc:
                await peer.recv()
            assert exc.value.rcvd.code == 1008


@pytest.mark.asyncio
async def test_one_connection_heartbeat_serial_tabs_unicode_and_phase():
    async with running_relay() as relay:
        async with await hello(relay) as peer:
            async with connect(f"ws://127.0.0.1:{relay.port}", origin=ORIGIN) as second:
                with pytest.raises(ConnectionClosed) as exc:
                    await second.recv()
                assert exc.value.rcvd.reason == "browser_relay_connection_in_use"
            await peer.send('{"type":"ping"}')
            assert json.loads(await peer.recv()) == {"type": "pong"}
            first = asyncio.create_task(relay.call({"action": "type", "tab_id": 11, "ref": "11:g2:r1", "text": "Élodie 李明 Ω"}))
            request = json.loads(await peer.recv())
            assert request["arguments"]["text"] == "Élodie 李明 Ω"
            second_call = asyncio.create_task(relay.call({"action": "snapshot", "tab_id": 22}))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(peer.recv(), .03)
            await reply(peer, request, {"tab": {"id": 11}, "action": "type", "snapshot": {"generation": 2}})
            assert (await first)["result"]["tab"]["id"] == 11
            next_request = json.loads(await peer.recv())
            assert next_request["id"] != request["id"] and next_request["arguments"]["tab_id"] == 22
            await peer.send(json.dumps({"type": "result", "id": next_request["id"], "ok": False,
                                       "error": {"code": "readback_failed", "message": "Action confirmed, readback failed"}, "phase": "action_confirmed"}))
            assert (await second_call)["phase"] == "action_confirmed"


@pytest.mark.asyncio
async def test_timeout_late_id_isolation_no_replay_and_new_connection():
    async with running_relay(timeout=.08) as relay:
        peer = await hello(relay)
        call = asyncio.create_task(relay.call({"action": "click", "tab_id": 1, "ref": "g1:r1"}))
        old = json.loads(await peer.recv())
        assert (await call)["phase"] == "outcome_unknown"
        next_call = asyncio.create_task(relay.call({"action": "snapshot", "tab_id": 2}))
        new = json.loads(await peer.recv())
        await reply(peer, old, {"stale": True})
        await reply(peer, new, {"fresh": True})
        assert (await next_call)["result"] == {"fresh": True}
        read = asyncio.create_task(relay.call({"action": "tabs"}))
        await peer.recv()
        assert (await read)["phase"] == "not_dispatched"
        active = asyncio.create_task(relay.call({"action": "navigate", "tab_id": 1, "url": "https://example.test"}))
        await peer.recv()
        queued = asyncio.create_task(relay.call({"action": "release", "tab_id": 2}))
        await asyncio.sleep(0)
        await peer.close()
        assert (await active)["phase"] == "outcome_unknown"
        assert (await queued)["phase"] == "not_dispatched"
        async with await hello(relay) as replacement:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(replacement.recv(), .03)
            fresh = asyncio.create_task(relay.call({"action": "tabs"}))
            request = json.loads(await replacement.recv())
            await reply(replacement, request, {"tabs": []})
            assert (await fresh)["result"] == {"tabs": []}


@pytest.mark.asyncio
async def test_malformed_response_fails_outstanding_without_replay():
    async with running_relay() as relay:
        async with await hello(relay) as peer:
            call = asyncio.create_task(relay.call({"action": "press", "tab_id": 1, "key": "Enter"}))
            request = json.loads(await peer.recv())
            await peer.send(json.dumps({"type": "result", "id": request["id"], "ok": True, "result": {}, "phase": "invented"}))
            result = await call
            assert result["error"]["code"] == "extension_disconnected" and result["phase"] == "outcome_unknown"


@pytest.mark.asyncio
async def test_port_collision_is_explicit_and_leaves_owner_alive():
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        with pytest.raises(RuntimeError, match="browser_relay_port_unavailable"):
            await BrowserRelay(port=port).start()
        assert owner.getsockname()[1] == port
