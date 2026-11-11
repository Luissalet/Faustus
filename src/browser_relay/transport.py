"""One extension connection, serialized commands, and no replay after dispatch."""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid

import anyio
from anyio.lowlevel import checkpoint

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .protocol import EXTENSION_ID, ORIGIN, READ_ACTIONS, failure, validate_arguments, validate_result


class BrowserRelay:
    def __init__(self, *, port: int = 7016, timeout: float = 30, hello_timeout: float = 5, peer_wait: float = 0):
        self.port = port
        self.timeout = timeout
        self.hello_timeout = hello_timeout
        # After a relay or browser restart the extension comes back on its own within seconds;
        # a call made in that gap waits for it instead of failing at once. Nothing is sent meanwhile.
        self.peer_wait = max(0.0, float(peer_wait))
        self._server = None
        self._peer = None
        self._ready = False
        self._serial = asyncio.Lock()
        self._pending = {}
        self._recordings = {}

    async def start(self):
        try:
            self._server = await serve(self._connection, "127.0.0.1", self.port,
                                       origins=[ORIGIN], max_size=None)
        except OSError as exc:
            raise RuntimeError(f"browser_relay_port_unavailable: 127.0.0.1:{self.port}: {exc}") from exc
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @staticmethod
    def _lost(action: str, code: str, message: str):
        phase = "not_dispatched" if action in READ_ACTIONS else "outcome_unknown"
        return failure(code, message, phase)

    async def _connection(self, peer):
        if self._peer is not None:
            await peer.close(1008, "browser_relay_connection_in_use")
            return
        self._peer = peer
        try:
            hello = json.loads(await asyncio.wait_for(peer.recv(), self.hello_timeout))
            if hello != {"type": "hello", "protocol": 1, "extensionId": EXTENSION_ID} or type(hello.get("protocol")) is not int:
                raise ValueError("Invalid hello or protocol")
            await peer.send(json.dumps({"type": "ready", "connection_id": str(uuid.uuid4())}))
            self._ready = True
            async for raw in peer:
                value = json.loads(raw)
                if value == {"type": "ping"}:
                    await peer.send('{"type":"pong"}')
                    continue
                if value.get("type") == "recording_frame":
                    self._recording_frame(value)
                    continue
                result = validate_result(value)
                pending = self._pending.get(value["id"])
                if pending is not None:
                    future, _ = pending
                    if not future.done():
                        future.set_result(result)
                # Expired/duplicate ids are never reassigned to another call.
        except (ValueError, TypeError, asyncio.TimeoutError):
            await peer.close(1008, "browser_relay_protocol_error")
        except ConnectionClosed:
            pass
        finally:
            if self._peer is peer:
                self._peer = None
                self._ready = False
                for future, action in self._pending.values():
                    if not future.done():
                        future.set_result(self._lost(action, "extension_disconnected", "Extension disconnected; no command was replayed"))

    def _recording_frame(self, value):
        tab_id, frame = value.get("tab_id"), value.get("frame")
        capture = self._recordings.get(tab_id) if type(tab_id) is int else None
        if capture is None:
            return
        if not isinstance(frame, dict) or not isinstance(frame.get("data"), str):
            raise ValueError("Invalid recording frame")
        timestamp = frame.get("timestamp")
        if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
            raise ValueError("Invalid recording timestamp")
        frames = capture["frames"]
        if frames and timestamp < frames[-1]["timestamp"]:
            raise ValueError("Recording timestamps must be ordered")
        frames.append(frame)
        # Keep the preceding still image too, so a quiet tab fills the beginning
        # of the minute instead of appearing to start at the next animation.
        older = [item for item in frames if item["timestamp"] < timestamp - 60]
        capture["frames"] = older[-1:] + [item for item in frames if item["timestamp"] >= timestamp - 60]

    def recent_video(self, tab_id):
        capture = self._recordings.get(tab_id)
        if not capture or not capture["frames"]:
            return None
        frames = capture["frames"]
        end = max(time.time(), frames[-1]["timestamp"])
        start = max(frames[0]["timestamp"], end - 59.8)
        anchor = next((frame for frame in reversed(frames) if frame["timestamp"] <= start), frames[0])
        recent = [{**anchor, "timestamp": start}] + [frame for frame in frames if start < frame["timestamp"] < end]
        if end > start:
            recent.append({**frames[-1], "timestamp": end})
        return {**capture, "frames": recent, "last_minute": True}

    def forget_video(self, tab_id):
        self._recordings.pop(tab_id, None)

    async def _retire(self, peer, reason: str):
        # Retire the captured peer only, even if a replacement becomes ready.
        if self._peer is peer:
            self._ready = False
        # MCP cancellation is an AnyIO cancel scope: an unshielded close await
        # would be cancelled again. Finish the existing cleanup in this owner.
        with anyio.CancelScope(shield=True):
            try:
                await peer.close(1011, reason)
            except ConnectionClosed:
                pass

    async def call(self, arguments: object) -> dict:
        try:
            args = validate_arguments(arguments)
        except ValueError as exc:
            return failure("invalid_arguments", str(exc))
        peer = self._peer if self._ready else None
        if peer is None and self.peer_wait > 0:
            deadline = asyncio.get_running_loop().time() + self.peer_wait
            while peer is None and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
                peer = self._peer if self._ready else None
        if peer is None:
            return failure("extension_unavailable", "Connect the browser session extension; no browser was launched")
        async with self._serial:
            # Observe cancellation before creating/sending a command task.
            await checkpoint()
            if self._peer is not peer or not self._ready:
                return failure("extension_disconnected", "The original extension connection ended before dispatch")
            request_id = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = (future, args["action"])
            previous_recording = self._recordings.get(args.get("tab_id"))
            if args["action"] == "recording_start":
                self._recordings[args["tab_id"]] = {"mime": "video/webm", "cursor": args.get("cursor", False), "frames": []}
            try:
                # A send failure can be partial: mutations are conservatively unknown.
                await asyncio.wait_for(peer.send(json.dumps({"type": "command", "id": request_id, "arguments": args}, ensure_ascii=False)), self.timeout)
                result = await asyncio.wait_for(asyncio.shield(future), self.timeout)
                if args["action"] == "recording_start" and not result.get("ok"):
                    self._recordings.pop(args["tab_id"], None)
                    if previous_recording is not None:
                        self._recordings[args["tab_id"]] = previous_recording
                return result
            except asyncio.CancelledError:
                await self._retire(peer, "browser_relay_command_cancelled")
                raise
            except asyncio.TimeoutError:
                # The extension may still await CDP: retire its serialized queue before another call.
                await self._retire(peer, "browser_relay_command_timeout")
                return self._lost(args["action"], "command_timeout", "No response before timeout; the expired connection was closed; no command was replayed")
            except ConnectionClosed:
                return self._lost(args["action"], "extension_disconnected", "Extension disconnected during dispatch; no command was replayed")
            finally:
                self._pending.pop(request_id, None)
                if not future.done():
                    future.cancel()
