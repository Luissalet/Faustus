"""Keep Windows Proactor listeners alive when a queued peer resets (WinError 64).

Uses CPython's private IOCP registration contract, also used by its PSF-licensed
asyncio.windows_events.IocpProactor.accept. No standard-library object is patched;
only Faustus' loop-owned proactor receives this accept implementation.
"""
from __future__ import annotations

import asyncio
import socket
import struct
import sys
import types


def _accept_peer(self, listener):
    from asyncio.windows_events import _overlapped

    self._register_with_iocp(listener)

    async def ready():
        while True:
            peer = self._get_accept_socket(listener.family)
            try:
                overlapped = _overlapped.Overlapped(0)
                overlapped.AcceptEx(listener.fileno(), peer.fileno())

                def finish_accept(transferred, key, operation):
                    operation.getresult()
                    peer.setsockopt(socket.SOL_SOCKET,
                                    _overlapped.SO_UPDATE_ACCEPT_CONTEXT,
                                    struct.pack('@P', listener.fileno()))
                    peer.settimeout(listener.gettimeout())
                    return peer, peer.getpeername()

                return await self._register(overlapped, listener, finish_accept)
            except OSError as exc:
                peer.close()
                if getattr(exc, 'winerror', None) != 64 or listener.fileno() == -1:
                    raise
                # A failed peer is not a failed listener. Yield before rearming
                # so a queue of resets cannot monopolise the event loop.
                await asyncio.sleep(0)
            except BaseException:
                peer.close()
                raise

    return self._loop.create_task(ready())


def install_accept_retry(loop) -> bool:
    """Install once on this loop, without replacing its IOCP or subprocesses."""
    if sys.platform != 'win32':
        return False
    from asyncio.windows_events import IocpProactor

    proactor = getattr(loop, '_proactor', None)
    if not isinstance(proactor, IocpProactor):
        return False
    if not getattr(proactor, '_faustus_accept_retry', False):
        proactor.accept = types.MethodType(_accept_peer, proactor)
        proactor._faustus_accept_retry = True
    return True


def configure_windows_asyncio() -> None:
    """Cover both import-before-run and Uvicorn's import-inside-run startup."""
    if sys.platform != 'win32':
        return

    class AcceptResetPolicy(asyncio.WindowsProactorEventLoopPolicy):
        def new_event_loop(self):
            loop = super().new_event_loop()
            install_accept_retry(loop)
            return loop

    asyncio.set_event_loop_policy(AcceptResetPolicy())
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        # Uvicorn may already have constructed the loop before importing app.
        # This precedes its create_server, so its first accept is also covered.
        install_accept_retry(running)
