"""Actual MCP stdio/WS cancellation; peer/CDP expressly simulated, no Browser."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys

import anyio
import pytest
from websockets.asyncio.client import connect
from src.browser_relay.protocol import EXTENSION_ID, ORIGIN
from src.browser_relay.server import save_download
from src.browser_relay.transport import BrowserRelay
from test_mcp import ROOT, StdioClient


async def with_mcp(run):
    process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT / "src/browser_relay/server.py"),
        "--port", "0", "--timeout", "2", cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    client, peers, drain = StdioClient(process), [], None
    async def stderr_tail():
        while line := await process.stderr.readline():client.stderr.append(line.decode().rstrip())
    try:
        line = (await asyncio.wait_for(process.stderr.readline(), 10)).decode().strip()
        assert line.startswith("browser_relay_listening=127.0.0.1:") and not line.endswith(":7016")
        url = line.replace("browser_relay_listening=", "ws://")
        drain = asyncio.create_task(stderr_tail())
        await client.rpc("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"file-cancellation-tests","version":"1"}})
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        await process.stdin.drain()
        async def open_peer():
            peer=await connect(url,origin=ORIGIN);peers.append(peer)
            await peer.send(json.dumps({"type":"hello","protocol":1,"extensionId":EXTENSION_ID}))
            assert json.loads(await asyncio.wait_for(peer.recv(),3))["type"]=="ready"
            return peer
        peer=await open_peer()
        await run(client,peer,open_peer)
    finally:
        for peer in peers:await peer.close()
        if process.stdin:process.stdin.close()
        try:await asyncio.wait_for(process.wait(),10)
        except asyncio.TimeoutError:
            process.kill();await process.wait();raise
        if drain is not None:await drain
    assert process.returncode==0
    assert not any("Traceback" in line for line in client.stderr)
    assert await process.stdout.read()==b""


async def acknowledge(peer,command,result):
    await peer.send(json.dumps({"type":"result","id":command["id"],"ok":True,"phase":"readback_complete","result":result}))


@pytest.mark.asyncio
@pytest.mark.parametrize("action",["upload","download"])
async def test_mcp_cancel_retires_original_file_peer_and_only_next_call_reaches_replacement(tmp_path,action):
    path=tmp_path/"input-你.bin";path.write_bytes(b"REAL INPUT")
    output=tmp_path/"not-created.bin"
    args={"action":action,"tab_id":2,"ref":"current"}
    args.update({"files":[str(path)]} if action=="upload" else {"output_path":str(output)})
    async def run(client,peer,open_peer):
        ident=client.next_id+1
        pending=asyncio.create_task(client.call(args))
        try:
            command=json.loads(await asyncio.wait_for(peer.recv(),3));assert command["arguments"]==args
            client.process.stdin.write((json.dumps({"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":ident,"reason":"cancel dispatched file command"}})+"\n").encode())
            await client.process.stdin.drain()
            with pytest.raises(AssertionError,match="Request cancelled"):await asyncio.wait_for(pending,3)
            await asyncio.wait_for(peer.wait_closed(),3)
            assert peer.close_code==1011 and peer.close_reason=="browser_relay_command_cancelled"
            fresh=await open_peer()
            # The abandoned id is ignored; ping confirms it was processed without replay.
            await acknowledge(fresh,command,{"abandoned":True})
            await fresh.send('{"type":"ping"}')
            assert json.loads(await asyncio.wait_for(fresh.recv(),3))=={"type":"pong"}
            following=asyncio.create_task(client.call({"action":"tabs"}))
            try:
                next_command=json.loads(await asyncio.wait_for(fresh.recv(),3))
                assert next_command["arguments"]=={"action":"tabs"} and next_command["id"]!=command["id"]
                await acknowledge(fresh,next_command,{"tabs":[]})
                reply=await following
                assert not reply["isError"] and reply["structuredContent"]["result"]=={"tabs":[]}
            finally:
                if not following.done():following.cancel()
                await asyncio.gather(following,return_exceptions=True)
            assert path.read_bytes()==b"REAL INPUT" and not output.exists()
        finally:
            if not pending.done():pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)
    await with_mcp(run)


@pytest.mark.asyncio
async def test_cancel_scope_before_dispatch_sends_nothing_and_preserves_ready_peer():
    class Peer:
        def __init__(self):self.commands=[];self.closed=False
        async def send(self,value):self.commands.append(value)
        async def close(self,*args):self.closed=True
    relay=BrowserRelay();peer=Peer();relay._peer=peer;relay._ready=True
    with anyio.CancelScope() as scope:
        scope.cancel()
        await relay.call({"action":"upload","tab_id":2,"ref":"r","files":["C:/input"]})
    assert scope.cancelled_caught and not peer.commands and not peer.closed
    assert relay._ready and not relay._pending and not relay._serial.locked()


@pytest.mark.asyncio
async def test_cancel_scope_cleanup_finishes_on_original_peer_and_does_not_retire_replacement():
    class Peer:
        def __init__(self):self.sent=asyncio.Event();self.closing=asyncio.Event();self.finish=asyncio.Event();self.closed=False
        async def send(self,value):self.sent.set()
        async def close(self,*args):self.closing.set();await self.finish.wait();self.closed=True
    relay=BrowserRelay();original=Peer();fresh=Peer();relay._peer=original;relay._ready=True
    scopes=[];done=asyncio.Event()
    async def invoke():
        try:
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                await relay.call({"action":"download","tab_id":2,"ref":"r","output_path":"C:/new"})
        finally:done.set()
    task=asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(original.sent.wait(),3);scopes[0].cancel()
        await asyncio.wait_for(original.closing.wait(),3)
        assert not relay._ready and not done.is_set()
        relay._peer=fresh;relay._ready=True
        original.finish.set();await asyncio.wait_for(task,3)
        assert scopes[0].cancelled_caught and original.closed and not fresh.closed
        assert relay._peer is fresh and relay._ready and not relay._pending and not relay._serial.locked()
    finally:
        original.finish.set();fresh.finish.set()
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.asyncio
async def test_same_length_input_replacement_after_dispatch_never_gets_selection_receipt(tmp_path):
    path = tmp_path / "input-你.bin"
    path.write_bytes(b"OLD")
    async def run(client, peer, _open_peer):
        pending = asyncio.create_task(client.call({"action": "upload", "tab_id": 2,
                                                  "ref": "current", "files": [str(path)]}))
        command = json.loads(await asyncio.wait_for(peer.recv(), 3))
        path.write_bytes(b"NEW")
        await acknowledge(peer, command, {"tab": {"id": 2}, "upload": {"selected": 1}})
        result = await pending
        assert result["isError"]
        payload = result["structuredContent"]
        assert payload["error"]["code"] == "upload_input_changed"
        assert payload["phase"] == "action_confirmed" and "result" not in payload
        assert path.read_bytes() == b"NEW"
        next_call = asyncio.create_task(client.call({"action": "tabs"}))
        following = json.loads(await asyncio.wait_for(peer.recv(), 3))
        assert following["id"] != command["id"] and following["arguments"] == {"action": "tabs"}
        await acknowledge(peer, following, {"tabs": []})
        assert not (await next_call)["isError"]
    await with_mcp(run)

@pytest.mark.asyncio
async def test_output_created_between_preflight_and_reply_is_preserved(tmp_path):
    path = tmp_path / "race.bin"
    async def run(client, peer, _open_peer):
        pending = asyncio.create_task(client.call({"action": "download", "tab_id": 2,
                                                  "ref": "current", "output_path": str(path)}))
        command = json.loads(await asyncio.wait_for(peer.recv(), 3))
        path.write_bytes(b"OTHER OWNER")
        await acknowledge(peer, command, {"download": {"name": "new.bin", "mime": "application/octet-stream",
                           "bytes": 3, "parts": [base64.b64encode(b"NEW").decode()]}})
        result = await pending
        assert result["isError"] and result["structuredContent"]["error"]["code"] == "download_write_failed"
        assert result["structuredContent"]["phase"] == "action_confirmed"
        assert "result" not in result["structuredContent"] and path.read_bytes() == b"OTHER OWNER"
        assert '"parts"' not in result["content"][0]["text"]
    await with_mcp(run)

def test_silent_short_write_fails_readback_and_never_claims_complete(tmp_path, monkeypatch):
    path, original_open = tmp_path / "short.bin", Path.open
    class ShortWriter:
        def __enter__(self):
            self.handle = original_open(path, "xb")
            return self
        def write(self, data):
            return self.handle.write(data[:1])
        def __exit__(self, *args):
            self.handle.close()
    def opened(self, mode="r", *args, **kwargs):
        return ShortWriter() if self == path and mode == "xb" else original_open(self, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    result = save_download({"ok": True, "phase": "readback_complete", "result": {
        "download": {"name": "x", "mime": "", "bytes": 3, "parts": ["WFla"]}}}, str(path))
    assert not result["ok"] and result["error"]["code"] == "download_write_failed"
    assert result["phase"] == "action_confirmed" and "result" not in result
    assert path.read_bytes() == b"X"
