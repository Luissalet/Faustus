"""Request-scoped initial response budget, inherited by detached agent tasks."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import math
from typing import Optional

from fastapi import HTTPException, Request

MIN_SECONDS = 5.0
MAX_SECONDS = 900.0
FIELD = "initial_response_timeout_s"
_OVERRIDE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "initial_response_timeout_s", default=None
)


def parse(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("initial_response_timeout_s must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("initial_response_timeout_s must be a number") from None
    if not math.isfinite(number) or not MIN_SECONDS <= number <= MAX_SECONDS:
        raise ValueError("initial_response_timeout_s must be between 5 and 900 seconds")
    return number


def effective(default: Optional[float]) -> Optional[float]:
    """Keep endpoint defaults when absent; never change saved preferences."""
    value = _OVERRIDE.get()
    return value if value is not None else default


async def scoped_stream(source, seconds):
    """Keep deferred comparison streams in scope; one task owns cleanup."""
    queue = asyncio.Queue(maxsize=1)
    async def produce():
        token = _OVERRIDE.set(seconds)
        try:
            async for chunk in source:
                await queue.put(("chunk", chunk))
            await queue.put(("end", None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("error", exc))
        finally:
            try:
                await source.aclose()
            finally:
                _OVERRIDE.reset(token)
    worker = asyncio.create_task(produce())
    try:
        while True:
            kind, value = await queue.get()
            if kind == "end":
                return
            if kind == "error":
                raise value
            yield value
    finally:
        if not worker.done():
            worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


def request_budget(endpoint):
    """Parse before any session mutation; reset parent context after dispatch.

    Detached tasks inherit their own ContextVar copy. The response headers
    record explicit overrides without changing the stream or approvals.
    """
    @functools.wraps(endpoint)
    async def wrapped(request: Request, *args, **kwargs):
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            body = await request.json()
            value = body.get(FIELD) if isinstance(body, dict) else None
        else:
            body = await request.form()
            value = body.get(FIELD)
        try:
            seconds = parse(value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        from src.project_execution_defaults import for_session, merge
        try:
            preferences = for_session(request, str(body.get("session") or ""), verify=False)
            resolved = merge(preferences["defaults"], body)
            seconds = parse(resolved.get(FIELD))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        request.state.execution_defaults = resolved
        request.state.project_execution_defaults = preferences
        token = _OVERRIDE.set(seconds)
        try:
            response = await endpoint(request, *args, **kwargs)
            if hasattr(response, "body_iterator"):
                response.body_iterator = scoped_stream(response.body_iterator, seconds)
            if seconds is not None:
                response.headers["X-Faustus-Initial-Response-Timeout-S"] = str(seconds)
            if preferences.get("project_id"):
                response.headers["X-Faustus-Execution-Defaults-Revision"] = str(preferences["revision"])
            return response
        finally:
            _OVERRIDE.reset(token)
    return wrapped
