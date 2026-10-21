"""Short-lived event loops that own the HTTP clients they create.

Task #1093: callers that today use bare ``asyncio.run`` must go through this
helper so the pool is closed on the owner loop before the loop itself closes.
Borrowed / injected clients are never closed here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def close_loop_http_clients(loop: Any = None) -> None:
    """Await aclose for factory-owned clients bound to ``loop`` (or the running loop)."""
    from src.llm_core import close_owned_http_clients

    await close_owned_http_clients(loop)


def run(coro: Coroutine[Any, Any, T], *, close_http: bool = True) -> T:
    """Run ``coro`` to completion, then close owned HTTP on that loop before it ends.

    Uses ``asyncio.Runner`` so ContextVars / result / cancellation match
    ``asyncio.run``. Never use this on the shared ASGI loop.
    """

    async def _runner() -> T:
        primary: BaseException | None = None
        result: T | None = None
        try:
            result = await coro
        except BaseException as exc:  # noqa: BLE001 - re-raised after cleanup
            primary = exc
        if close_http:
            try:
                await close_loop_http_clients()
            except Exception as close_exc:  # noqa: BLE001
                logger.warning("owned_loop HTTP close failed: %r", close_exc)
                if primary is None:
                    raise
        if primary is not None:
            raise primary
        return result  # type: ignore[return-value]

    with asyncio.Runner() as runner:
        return runner.run(_runner())
