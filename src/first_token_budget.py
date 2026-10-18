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
    """Keep deferred comparison streams in scope; one task owns cleanup.

    Every way the producer can end reaches the consumer: a chunk, the end, an
    error, or a cancellation raised by the source itself (an upstream call that
    was stopped). Only a cancellation of the producer task (the consumer going
    away) ends it silently. Without that, a source that raised CancelledError
    left the consumer waiting on an empty queue forever.
    """
    queue = asyncio.Queue(maxsize=1)
    async def produce():
        token = _OVERRIDE.set(seconds)
        try:
            async for chunk in source:
                await queue.put(("chunk", chunk))
            await queue.put(("end", None))
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            await queue.put(("cancelled", None))
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
            if kind == "cancelled":
                raise asyncio.CancelledError()
            yield value
    finally:
        if not worker.done():
            worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass


async def _request_input(request: Request):
    if "application/json" in request.headers.get("content-type", "").lower():
        return await request.json()
    # Chat text is bounded by the selected model's context budget. Starlette's
    # default 1 MiB text-part limit otherwise rejects it before that calculation.
    # Keep the parser's file/field counts and upload-file validation unchanged.
    return await request.form(max_part_size=math.inf)


async def request_seconds(request: Request) -> Optional[float]:
    """Validate the native JSON/form field without FastAPI's early Form parse."""
    body = await _request_input(request)
    value = body.get(FIELD) if hasattr(body, "get") else None
    try:
        return parse(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def request_budget(endpoint):
    """Parse before any session mutation; reset parent context after dispatch.

    Detached tasks inherit their own ContextVar copy. The response headers
    record explicit overrides without changing the stream or approvals.
    """
    @functools.wraps(endpoint)
    async def wrapped(request: Request, *args, **kwargs):
        body = await _request_input(request)
        seconds = await request_seconds(request)
        from src.project_execution_defaults import for_session, merge
        try:
            preferences = for_session(request, str(body.get("session") or ""), verify=False)
            from core.database import get_session_reasoning_effort
            from src.auth_helpers import effective_user
            sid = str(body.get("session") or "")
            requested_effort = body.get("reasoning_effort")
            explicit_effort = requested_effort is not None and str(requested_effort).strip() != ""
            request.state.reasoning_effort_explicit = explicit_effort
            explicit = body
            if explicit_effort and str(requested_effort).strip().lower() == "auto":
                # `auto` means inherit project/global defaults, while explicitly
                # suppressing the saved session preference for this turn.
                explicit = dict(body)
                explicit.pop("reasoning_effort", None)
            session_effort = get_session_reasoning_effort(sid, effective_user(request)) if sid else None
            session_defaults = {"reasoning_effort": session_effort} if session_effort and not explicit_effort else None
            resolved = merge(preferences["defaults"], explicit, session_defaults)
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
