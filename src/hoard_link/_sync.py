"""A synchronous facade for apps whose own code is not async.

Runs a private event loop in a background thread and forwards every call
through :func:`asyncio.run_coroutine_threadsafe`. One facade per
:class:`~hoard_link.link.Link`; the loop is started lazily on first use.

The facade drives a private *twin* of the parent ``Link`` (same config,
its own probe cache and, when the parent created its own HTTP client, its
own ``httpx.AsyncClient``). An ``httpx.AsyncClient`` binds its connection
pool to the first event loop that uses it, so sharing the parent's client
between the app's loop and this private loop fails with "bound to a
different event loop" as soon as an app mixes ``await link.chat()`` and
``link.sync.chat()``. A client the app injected itself is shared as given
(the app owns its lifecycle; a transport-level mock has no loop state).

It is safe to call from a thread that is running its own event loop (the
call blocks that thread until the result is ready — prefer ``await`` there)
and it refuses, instead of deadlocking, to be called from its own loop
thread (e.g. from inside an ``on_progress`` callback).
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

if TYPE_CHECKING:
    from .link import Link


class SyncFacade:
    def __init__(self, link: "Link"):
        self._link = link
        self._inner: Optional["Link"] = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()
            box: dict[str, asyncio.AbstractEventLoop] = {}

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                box["loop"] = loop
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=runner, daemon=True, name="hoard-link-sync")
            thread.start()
            ready.wait()
            self._loop = box["loop"]
            self._thread = thread
            return self._loop

    def _twin(self) -> "Link":
        with self._start_lock:
            if self._inner is None:
                from .link import Link

                parent = self._link
                inner = Link(parent.config, client=None if parent._owns_client else parent._client)
                inner._now = parent._now
                inner._sleep = parent._sleep
                self._inner = inner
            return self._inner

    def _run(self, call: Callable[["Link"], Coroutine[Any, Any, Any]]) -> Any:
        loop = self._ensure_loop()
        if threading.current_thread() is self._thread:
            raise RuntimeError(
                "link.sync.* was called from Hoard Link's own event loop thread "
                "(e.g. inside a callback); await the async method instead"
            )
        return asyncio.run_coroutine_threadsafe(call(self._twin()), loop).result()

    def resolve(self, capability: str, task: Optional[str] = None):
        return self._run(lambda link: link.resolve(capability, task))

    def chat(self, *args: Any, **kwargs: Any):
        return self._run(lambda link: link.chat(*args, **kwargs))

    def embed(self, *args: Any, **kwargs: Any):
        return self._run(lambda link: link.embed(*args, **kwargs))

    def tts(self, *args: Any, **kwargs: Any):
        return self._run(lambda link: link.tts(*args, **kwargs))

    def status(self, task: Optional[str] = None):
        return self._run(lambda link: link.status(task))

    def wait_idle(self, *args: Any, **kwargs: Any):
        return self._run(lambda link: link.wait_idle(*args, **kwargs))

    def close(self, timeout: float = 2.0) -> None:
        loop, inner = self._loop, self._inner
        if loop is not None and inner is not None and inner._owns_client:
            if threading.current_thread() is not self._thread:
                try:
                    asyncio.run_coroutine_threadsafe(inner._client.aclose(), loop).result(timeout)
                except Exception:
                    pass
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None
        self._inner = None
