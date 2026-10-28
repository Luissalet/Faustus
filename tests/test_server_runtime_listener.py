"""The managed server must keep (or regain) its listening socket.

On 28-10-2026 the web server stayed alive but stopped accepting connections
after ``OSError: [WinError 64]`` in ``IocpProactor.accept`` ("Accept failed on a
socket"): asyncio closes the listening socket and never reopens it.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import asyncio
import socket
import sys
import threading
import time

import pytest
import server_runtime as runtime


class Echo(asyncio.Protocol):
    def connection_made(self,transport):self.transport=transport
    def data_received(self,data):self.transport.write(data)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0));return probe.getsockname()[1]


class LoopServer:
    """An asyncio echo server in a thread, shaped like the bits of uvicorn.Server we use."""
    def __init__(self,loop):
        self.loop=loop;self.port=free_port()
        self.config=SimpleNamespace(backlog=128)
        self.started=False;self.should_exit=False;self.servers=[]
        self.ready=threading.Event()
        self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()
        assert self.ready.wait(5)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        async def boot():
            self.servers=[await self.loop.create_server(Echo,host="127.0.0.1",port=self.port,backlog=128)]
            self.started=True;self.ready.set()
        self.loop.run_until_complete(boot())
        self.loop.run_forever()

    def close(self):
        async def shut():
            for item in self.servers:item.close()
        asyncio.run_coroutine_threadsafe(shut(),self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop);self.thread.join(5)
        self.loop.close()


def talk(port,payload=b"ping"):
    connection=socket.create_connection(("127.0.0.1",port),timeout=3)
    connection.sendall(payload);assert connection.recv(16)==payload
    return connection


def wait_until(check,seconds=8):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if check():return True
        time.sleep(.05)
    return False


@pytest.fixture
def served():
    server=LoopServer(asyncio.new_event_loop())
    yield server
    server.close()


def start_watchdog(server):
    finished=threading.Event()
    thread=threading.Thread(target=runtime.watch_listener,args=(server,server.port,finished,.05,2),daemon=True)
    thread.start()
    return finished,thread


def test_dead_listener_is_detected_and_rebuilt_without_dropping_connections(served):
    running=talk(served.port)
    for sock in list(served.servers[0]._sockets):sock.close()  # what a failed accept leaves behind
    assert runtime._dead_listener(served) and not runtime.listening(served.port)
    finished,thread=start_watchdog(served)
    try:
        assert wait_until(lambda:runtime.listening(served.port)),"listener was not reopened"
        assert not runtime._dead_listener(served)
        running.sendall(b"still-here");assert running.recv(16)==b"still-here"  # running turn survived
        talk(served.port,b"fresh").close()
    finally:
        finished.set();thread.join(2);running.close()


def test_healthy_listener_is_left_alone(served):
    before=served.servers[0]
    finished,thread=start_watchdog(served)
    time.sleep(.5);finished.set();thread.join(2)
    assert served.servers[0] is before and runtime.listening(served.port)


@pytest.mark.skipif(sys.platform!="win32",reason="IocpProactor is Windows-only")
def test_winerror_64_in_proactor_accept_does_not_leave_the_port_deaf():
    loop=asyncio.new_event_loop()
    assert isinstance(loop,asyncio.ProactorEventLoop)
    proactor=loop._proactor
    real_accept=proactor.accept;failed=[]
    def accept(listener):
        if not failed:
            failed.append(True)
            future=loop.create_future()
            future.set_exception(OSError(None,"The specified network name is no longer available",None,64))
            return future
        return real_accept(listener)
    proactor.accept=accept
    served=LoopServer(loop)
    finished=thread=None
    try:
        assert wait_until(lambda:bool(failed)),"the accept error was not injected"
        assert wait_until(lambda:not runtime.listening(served.port),3),"asyncio did not close the listener"
        finished,thread=start_watchdog(served)
        assert wait_until(lambda:runtime.listening(served.port)),"listener was not reopened after WinError 64"
        talk(served.port).close()
    finally:
        if finished:finished.set();thread.join(2)
        served.close()


def test_rebind_without_a_protocol_factory_changes_nothing():
    server=SimpleNamespace(servers=[SimpleNamespace(close=Mock())],config=SimpleNamespace(backlog=1))
    assert asyncio.run(runtime.rebind_listener(server,free_port())) is False
    server.servers[0].close.assert_not_called()


def test_watchdog_survives_a_failed_rebuild():
    class Stuck:
        started=True;should_exit=False
        config=SimpleNamespace(backlog=1)
        def __init__(self):self.servers=[SimpleNamespace(get_loop=Mock(side_effect=RuntimeError("no loop")))]
    finished=threading.Event()
    thread=threading.Thread(target=runtime.watch_listener,args=(Stuck(),free_port(),finished,.02,1),daemon=True)
    thread.start();time.sleep(.3)
    assert thread.is_alive()
    finished.set();thread.join(2);assert not thread.is_alive()


def old_record():return {"pid":1,"created":10.0,"token":"t","port":7000,"owner":"web"}


def test_old_server_that_refuses_connections_is_unreachable(monkeypatch):
    process=Mock();process.create_time.return_value=time.time()-3600
    monkeypatch.setattr(runtime,"owned_process",lambda _:process)
    monkeypatch.setattr(runtime,"listening",lambda _:False)
    monkeypatch.setattr(runtime,"UNREACHABLE_SECONDS",.2)
    monkeypatch.setattr(runtime.time,"sleep",lambda _:None)
    assert runtime._unreachable_server(old_record(),7000) is True


def test_young_or_listening_server_is_not_unreachable(monkeypatch):
    process=Mock();process.create_time.return_value=time.time()-5
    monkeypatch.setattr(runtime,"owned_process",lambda _:process)
    monkeypatch.setattr(runtime,"listening",lambda _:False)
    assert runtime._unreachable_server(old_record(),7000) is False  # still starting up
    process.create_time.return_value=time.time()-3600
    monkeypatch.setattr(runtime,"listening",lambda _:True)
    assert runtime._unreachable_server(old_record(),7000) is False


def test_start_replaces_a_deaf_server(monkeypatch):
    monkeypatch.setattr(runtime,"launch_lock",nullcontext)
    records=[old_record(),{}]
    monkeypatch.setattr(runtime,"read_record",lambda:records.pop(0) if len(records)>1 else records[0])
    monkeypatch.setattr(runtime,"owned_process",lambda r:object() if r else None)
    monkeypatch.setattr(runtime,"_unreachable_server",lambda *_:True)
    retire=Mock(return_value=True);monkeypatch.setattr(runtime,"_retire_server",retire)
    monkeypatch.setattr(runtime,"listening",lambda _:True)
    monkeypatch.setattr(runtime,"healthy",lambda _:True)
    runtime.start(7000)
    retire.assert_called_once()
