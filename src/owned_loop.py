"""Short-lived event loops that own the HTTP clients they create.

Task #1093: callers that today use bare ``asyncio.run`` must go through this
helper so the pool is closed on the owner loop before the loop itself closes.
Borrowed / injected clients are never closed here.

This module is a RED stub until the lifecycle suite turns green.
"""
from __future__ import annotations

from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


async def close_loop_http_clients(loop: Any = None) -> None:
    """Await aclose for factory-owned clients bound to ``loop`` (or the running loop)."""
    raise NotImplementedError("1093: owner-loop HTTP close not implemented")


def run(coro: Coroutine[Any, Any, T], *, close_http: bool = True) -> T:
    """Run ``coro`` to completion, then drain own tasks/asyncgens and close owned HTTP."""
    raise NotImplementedError("1093: owned_loop.run not implemented")
