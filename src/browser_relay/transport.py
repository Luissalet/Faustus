"""One extension connection, serialized commands, and no replay after dispatch."""
from __future__ import annotations

import asyncio
import json
import uuid

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from .protocol import EXTENSION_ID, ORIGIN, READ_ACTIONS, failure, validate_arguments, validate_result


class BrowserRelay:
    def __init__(self, *, port: int = 7016, timeout: float = 30, hello_timeout: float = 5):
        self.port = port
        self.timeout = timeout
        self.hello_timeout = hello_timeout
        self._server = None
        self._peer = None
        self._ready = False
        self._serial = asyncio.Lock()
        self._pending = {}

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

    async def call(self, arguments: object) -> dict:
        try:
            args = validate_arguments(arguments)
        except ValueError as exc:
            return failure("invalid_arguments", str(exc))
        peer = self._peer if self._ready else None
        if peer is None:
            return failure("extension_unavailable", "Connect the browser session extension; no browser was launched")
        async with self._serial:
            if self._peer is not peer or not self._ready:
                return failure("extension_disconnected", "The original extension connection ended before dispatch")
            request_id = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = (future, args["action"])
            try:
                # A send failure can be partial: mutations are conservatively unknown.
                await asyncio.wait_for(peer.send(json.dumps({"type": "command", "id": request_id, "arguments": args}, ensure_ascii=False)), self.timeout)
                return await asyncio.wait_for(asyncio.shield(future), self.timeout)
            except asyncio.TimeoutError:
                return self._lost(args["action"], "command_timeout", "No response before timeout; no command was replayed")
            except ConnectionClosed:
                return self._lost(args["action"], "extension_disconnected", "Extension disconnected during dispatch; no command was replayed")
            finally:
                self._pending.pop(request_id, None)
                if not future.done():
                    future.cancel()
