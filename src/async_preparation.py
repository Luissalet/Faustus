"""Keep pure turn-preparation predicates off the event loop."""
import asyncio
from typing import Callable, Optional


def _check_cancel(pending_cancel: Optional[Callable]) -> None:
    try:
        reason = pending_cancel() if pending_cancel is not None else None
    except Exception:
        # Match the agent loop's existing authoritative cancellation poll.
        reason = None
    if reason:
        raise asyncio.CancelledError(reason)


async def run_preparation(function, *args, pending_cancel=None, **kwargs):
    """Await a pure predicate, preserving context and owning its worker to exit.

    Callers must not pass model/tool execution or shared-state mutations.
    A preparation builder may own a private payload until this await finishes.
    A running Python
    thread cannot be forcibly stopped: cancellation waits for this predicate
    to finish, including after repeated cancellation, and never advances the
    caller to its next stage. ``to_thread`` carries the caller's contextvars.
    """
    _check_cancel(pending_cancel)
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()  # Retrieve a late failure; cancellation still wins.
        raise
    _check_cancel(pending_cancel)
    return result
